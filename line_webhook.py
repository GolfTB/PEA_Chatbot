from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FollowEvent, JoinEvent
import sqlite3
import os
import sys
import json
import time
import re
import threading
from dotenv import load_dotenv

base_dir = os.path.dirname(os.path.abspath(__file__))
mqtt_dir = os.path.dirname(base_dir)
project_root = os.path.dirname(mqtt_dir)

for path in (mqtt_dir, project_root):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent import Agent


class LineRegister:
    def __init__(self, db_file: str = "employees.db"):
        load_dotenv(os.path.join(base_dir, ".env"))

        self.db_file = db_file
        self.app = Flask(__name__)

        self.channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
        self.channel_secret = os.environ.get("LINE_CHANNEL_SECRET")
        self.mqtt_host = os.environ.get("MQTT_HOST")
        self.mqtt_username = os.environ.get("MQTT_USERNAME")
        self.mqtt_password = os.environ.get("MQTT_PASSWORD")
        self.mqtt_topic = (os.environ.get("MQTT_TOPIC", "ai_timesheet") or "ai_timesheet").strip().strip(",")
        self.mqtt_to = (os.environ.get("MQTT_TO", "ai_timesheet") or "ai_timesheet").strip().strip(",")
        self.mqtt_from = (os.environ.get("MQTT_FROM", "line_webhook") or "line_webhook").strip().strip(",")
        self.mqtt_reply_topic = (os.environ.get("MQTT_REPLY_TOPIC", "ai_timesheet_reply") or "ai_timesheet_reply").strip().strip(",")
        self.mqtt_reply_to = (os.environ.get("MQTT_REPLY_TO", "line_webhook") or "line_webhook").strip().strip(",")
        self.bot_user_id = (os.environ.get("LINE_BOT_USER_ID") or "").strip()

        if not self.channel_access_token or not self.channel_secret:
            raise RuntimeError("กรุณาตั้งค่า LINE_CHANNEL_ACCESS_TOKEN และ LINE_CHANNEL_SECRET ในไฟล์ .env")

        self.line_bot_api = LineBotApi(self.channel_access_token)
        self.webhook_handler = WebhookHandler(self.channel_secret)
        self.agent = None
        self.agent_thread = None
        self.mqtt_enabled = False
        self._seen_reply_tokens = {}
        self._seen_reply_tokens_lock = threading.Lock()

        self._setup_mqtt()
        self._log_route_config()

        self._register_routes()
        self._register_handlers()

    def _log_route_config(self):
        self.app.logger.info(
            "MQTT route config: host=%s, request_topic=%s, request_to=%s, reply_topic=%s, reply_to=%s, mqtt_enabled=%s",
            self.mqtt_host,
            self.mqtt_topic,
            self.mqtt_to,
            self.mqtt_reply_topic,
            self.mqtt_reply_to,
            self.mqtt_enabled,
        )

    def _on_mqtt_message(self, client, userdata, message):
        try:
            raw_payload = message.payload.decode("utf-8", "strict")
            print(f"[MQTT-RAW] topic={message.topic} payload={raw_payload}")
            self.app.logger.info("[MQTT-RAW] topic=%s payload=%s", message.topic, raw_payload)

            if message.topic != self.mqtt_reply_topic:
                print(
                    f"[MQTT-RAW] skip topic mismatch incoming={message.topic} expected={self.mqtt_reply_topic}"
                )
                self.app.logger.info(
                    "[MQTT-RAW] skip topic mismatch incoming=%s expected=%s",
                    message.topic,
                    self.mqtt_reply_topic,
                )
                return

            self.app.logger.info("[MQTT-IN] topic=%s raw_payload=%s", message.topic, raw_payload)
            payload = json.loads(raw_payload)

            target = str(payload.get("to", "")).strip()
            if target and target != self.mqtt_reply_to:
                print(f"[MQTT-IN] skip to mismatch incoming={target} expected={self.mqtt_reply_to}")
                return

            message_topic = str(payload.get("topic", "")).strip()
            self.app.logger.info(
                "[MQTT-IN] parsed frm=%s to=%s topic=%s",
                payload.get("frm", ""),
                target,
                message_topic,
            )
            if message_topic and message_topic != "attendance_result":
                print(f"[MQTT-IN] skip payload topic={message_topic} expected=attendance_result")
                self.app.logger.info("Skip MQTT reply payload: unsupported topic=%s", message_topic)
                return

            contents = payload.get("contents", {})
            msg_content = contents.get("msg", {}) if isinstance(contents, dict) else {}
            if not isinstance(msg_content, dict):
                print("[MQTT-IN] skip contents.msg not object")
                self.app.logger.warning("Skip MQTT reply payload: contents.msg is not object")
                return

            reply_token = str(msg_content.get("rep", "")).strip()
            response_text = str(msg_content.get("res", "")).strip()
            line_uuid = str(msg_content.get("line_uuid", "")).strip()

            if not reply_token or not response_text:
                print(f"[MQTT-IN] skip missing rep/res token={'yes' if reply_token else 'no'} res={'yes' if response_text else 'no'}")
                self.app.logger.warning("Skip MQTT reply payload: missing rep/res")
                return

            token_prefix = f"{reply_token[:8]}..."
            print(f"[MQTT-IN] reply -> LINE token={token_prefix} chars={len(response_text)}")
            try:
                self.line_bot_api.reply_message(reply_token, TextSendMessage(text=response_text))
                print(f"[MQTT-IN] LINE reply success token={token_prefix}")
                self.app.logger.info(
                    "Forwarded AI reply to LINE via MQTT topic=%s token=%s",
                    self.mqtt_reply_topic,
                    token_prefix,
                )
            except LineBotApiError as exc:
                status_code = getattr(exc, "status_code", "unknown")
                print(f"[MQTT-IN] LINE reply failed status={status_code} token={token_prefix} error={exc}")
                self.app.logger.error(
                    "LINE reply failed status=%s token=%s error=%s",
                    status_code,
                    token_prefix,
                    exc,
                )
        except Exception as exc:
            print(f"[MQTT-IN] handler exception: {exc}")
            self.app.logger.error("Failed to process MQTT reply message: %s", exc)

    def _setup_mqtt(self):
        if not self.mqtt_host:
            self.app.logger.warning("MQTT_HOST not set, LINE Webhook will not forward messages to AI")
            return

        try:
            self.agent = Agent(
                topic="line_webhook",
                host=self.mqtt_host,
                username=self.mqtt_username,
                password=self.mqtt_password,
            )
            self.agent_thread = threading.Thread(
                target=self.agent.online,
                args=(self._on_mqtt_message,),
                daemon=True,
            )
            self.agent_thread.start()
            self.mqtt_enabled = True
            self.app.logger.info("MQTT agent started for LINE webhook")
        except Exception as exc:
            self.mqtt_enabled = False
            self.app.logger.error("Failed to start MQTT agent: %s", exc)

    def _register_routes(self):
        self.app.add_url_rule("/webhook", view_func=self.callback, methods=["POST"])
        self.app.add_url_rule("/callback", view_func=self.callback, methods=["POST"])
        self.app.add_url_rule("/webhook-test/line-webhook", view_func=self.callback, methods=["POST"])
        self.app.add_url_rule("/", view_func=self.health_check, methods=["GET"])
        self.app.add_url_rule("/healthz", view_func=self.health_check, methods=["GET"])

    def _register_handlers(self):
        # self.webhook_handler.add(FollowEvent)(self.handle_follow)
        self.webhook_handler.add(JoinEvent)(self.handle_join)
        self.webhook_handler.add(MessageEvent, message=TextMessage)(self.handle_text_message)

    def _row_to_employee(self, row):
        if not row:
            return None

        return {
            "user_id": row[0],
            "fullname": row[1],
            "position": row[2],
            "dept": row[3],
            "enabled": row[4],
            "line_uuid": row[5],
        }

    def get_employee_by_id(self, user_id: str):
        conn = sqlite3.connect(self.db_file)
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, fullname, position, dept, enabled, line_uuid FROM employees WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        conn.close()
        return self._row_to_employee(row)

    def get_employee_by_line_uuid(self, line_uuid: str):
        conn = sqlite3.connect(self.db_file)
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, fullname, position, dept, enabled, line_uuid FROM employees WHERE line_uuid = ?",
            (line_uuid,),
        )
        row = cur.fetchone()
        conn.close()
        return self._row_to_employee(row)

    def update_line_uuid(self, user_id: str, line_uuid: str):
        conn = sqlite3.connect(self.db_file)
        cur = conn.cursor()
        cur.execute("UPDATE employees SET line_uuid = ? WHERE user_id = ?", (line_uuid, user_id))
        conn.commit()
        conn.close()

    def is_employee_enabled(self, enabled_value: str):
        return str(enabled_value).strip().lower() in ["1", "true", "y", "yes"]

    def _get_bot_user_id(self) -> str:
        if self.bot_user_id:
            return self.bot_user_id

        try:
            info = self.line_bot_api.get_bot_info()
            self.bot_user_id = (getattr(info, "user_id", "") or "").strip()
        except Exception as exc:
            self.app.logger.warning("Cannot get bot user id for mention check: %s", exc)

        return self.bot_user_id

    def _is_bot_mentioned(self, event) -> bool:
        mention = getattr(event.message, "mention", None)
        if not mention:
            return False

        mentionees = mention.get("mentionees") if isinstance(mention, dict) else getattr(mention, "mentionees", None)
        if not mentionees:
            return False

        bot_user_id = self._get_bot_user_id()

        for mentionee in mentionees:
            if isinstance(mentionee, dict):
                if mentionee.get("isSelf") is True:
                    return True
                mentioned_user_id = (
                    (mentionee.get("userId") or mentionee.get("user_id") or "").strip()
                )
            else:
                if getattr(mentionee, "is_self", False) is True:
                    return True
                mentioned_user_id = (getattr(mentionee, "user_id", "") or "").strip()

            if bot_user_id and mentioned_user_id and mentioned_user_id == bot_user_id:
                return True

        return False

    def _is_duplicate_reply_token(self, reply_token: str, ttl_seconds: int = 120) -> bool:
        if not reply_token:
            return False

        now = time.time()
        with self._seen_reply_tokens_lock:
            expired_tokens = [token for token, ts in self._seen_reply_tokens.items() if now - ts > ttl_seconds]
            for token in expired_tokens:
                self._seen_reply_tokens.pop(token, None)

            if reply_token in self._seen_reply_tokens:
                return True

            self._seen_reply_tokens[reply_token] = now
            return False

    def _extract_employee_id(self, event, user_message: str) -> str:
        cleaned_message = user_message.strip()
        mention = getattr(event.message, "mention", None)

        mentionees = mention.get("mentionees") if isinstance(mention, dict) else getattr(mention, "mentionees", None)
        if isinstance(mentionees, list) and mentionees:
            ranges = []
            for mentionee in mentionees:
                if isinstance(mentionee, dict):
                    idx = mentionee.get("index")
                    length = mentionee.get("length")
                else:
                    idx = getattr(mentionee, "index", None)
                    length = getattr(mentionee, "length", None)

                if isinstance(idx, int) and isinstance(length, int) and idx >= 0 and length > 0:
                    ranges.append((idx, idx + length))

            if ranges:
                text_parts = []
                cursor = 0
                for start, end in sorted(ranges):
                    if start > cursor:
                        text_parts.append(cleaned_message[cursor:start])
                    cursor = max(cursor, end)
                if cursor < len(cleaned_message):
                    text_parts.append(cleaned_message[cursor:])
                cleaned_message = " ".join(part.strip() for part in text_parts if part.strip())

        number_candidates = re.findall(r"\d{5,16}", cleaned_message)
        if number_candidates:
            return number_candidates[0]

        return cleaned_message

    def callback(self):
        signature = request.headers.get("X-Line-Signature", "")
        body = request.get_data(as_text=True)

        self.app.logger.info(f"Request body: {body}")
        try:
            payload = json.loads(body)
            self.app.logger.info("LINE payload(JSON): %s", json.dumps(payload, ensure_ascii=False))
        except Exception:
            self.app.logger.warning("Request body is not valid JSON")

        try:
            self.webhook_handler.handle(body, signature)
        except InvalidSignatureError:
            self.app.logger.error("Invalid signature")
            abort(400)

        return "OK"

    # def handle_follow(self, event, *_):
    #     if self._is_duplicate_reply_token(event.reply_token):
    #         self.app.logger.info("Skip duplicate follow event reply_token=%s", event.reply_token[:8] + "...")
    #         return

    #     reply_text = (
    #         "🔐 ยินดีต้อนรับสู่ระบบยืนยันตัวตน\n\n"
    #         "กรุณาพิมพ์รหัสพนักงานของคุณเพื่อยืนยันตัวตน\n"
    #         "ตัวอย่าง: 12345"
    #     )

    #     self.line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

    def handle_join(self, event, *_):
        if self._is_duplicate_reply_token(event.reply_token):
            self.app.logger.info("Skip duplicate join event reply_token=%s", event.reply_token[:8] + "...")
            return

        reply_text = (
            "สวัสดีครับ! ขอบคุณที่เชิญผู้ช่วยฝ่ายบุคคลเข้ามาในกลุ่ม\n\n"
            "📌 วิธีใช้งาน:\n"
            "พิมพ์ @ชื่อบอท ตามด้วยข้อความ เช่น '@bot พรุ่งนี้ขอลากิจ 1 วัน'\n\n"
            "⚠️ สำหรับพนักงานที่ยังไม่เคยลงทะเบียน ให้ @ชื่อบอท แล้วพิมพ์ 'รหัสพนักงาน' ของคุณในกลุ่มนี้ก่อนนะครับ"
        )
        self.line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

    def handle_text_message(self, event, *_):
        if self._is_duplicate_reply_token(event.reply_token):
            self.app.logger.info("Skip duplicate text event reply_token=%s", event.reply_token[:8] + "...")
            return

        line_uuid = event.source.user_id
        user_message = event.message.text.strip()
        source_type = getattr(event.source, "type", "user")

        if source_type in {"group", "room"} and not self._is_bot_mentioned(event):
            self.app.logger.info("Skip message in %s: bot not mentioned", source_type)
            return

        existing_employee = self.get_employee_by_line_uuid(line_uuid)

        if existing_employee:
            if not self.mqtt_enabled or not self.agent:
                reply_text = "❌ ระบบ AI ยังไม่พร้อมใช้งาน กรุณาลองใหม่อีกครั้ง"
                self.line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
                return

            payload = {
                "frm": self.mqtt_from,
                "to": self.mqtt_to,
                "topic": "llm",
                "contents": {
                    "msg": {
                        "rep": event.reply_token,
                        "res": user_message,
                        "line_uuid": line_uuid,
                        "employee_id": existing_employee["user_id"],
                        "source_type": source_type,
                    }
                },
            }

            try:
                self.agent.pub(self.mqtt_topic, json.dumps(payload, ensure_ascii=False))
                self.app.logger.info("Forwarded message to MQTT topic=%s", self.mqtt_topic)
            except Exception as exc:
                self.app.logger.error("Failed to publish MQTT message: %s", exc)
                reply_text = "❌ ส่งข้อความไป AI ไม่สำเร็จ กรุณาลองใหม่อีกครั้ง"
                self.line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            return
        else:
            employee_id = self._extract_employee_id(event, user_message)
            employee = self.get_employee_by_id(employee_id)

            if employee:
                if employee["line_uuid"]:
                    reply_text = "❌ รหัสพนักงานนี้ถูกใช้ลงทะเบียนกับบัญชี LINE อื่นแล้ว\nกรุณาติดต่อฝ่าย IT"
                elif not self.is_employee_enabled(employee["enabled"]):
                    reply_text = "❌ บัญชีพนักงานนี้ถูกระงับการใช้งาน\nกรุณาติดต่อฝ่าย IT"
                else:
                    self.update_line_uuid(employee_id, line_uuid)
                    reply_text = (
                        "✅ ยืนยันตัวตนสำเร็จ!\n\n"
                        f"🎉 ยินดีต้อนรับ คุณ {employee['fullname']}\n"
                        f"📋 รหัสพนักงาน: {employee['user_id']}\n"
                        f"📌 ตำแหน่ง: {employee['position']}\n"
                        f"🏢 แผนก: {employee['dept']}"
                    )
            else:
                reply_text = "❌ ไม่พบรหัสพนักงานนี้ในระบบ\n\nกรุณาตรวจสอบและพิมพ์รหัสพนักงานอีกครั้ง\nตัวอย่าง: 12345"

        self.line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

    def health_check(self):
        return "LINE Register is running!"

    def run(self, host: str = "0.0.0.0", port: int = 5000, debug: bool = True):
        print("=" * 50)
        print("LINE Register")
        print("=" * 50)
        print(f"Webhook URL: http://localhost:{port}/webhook")
        print("ใช้ ngrok เพื่อสร้าง public URL:")
        print(f"  ngrok http {port}")
        print("=" * 50)
        self.app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    try:
        webhook_app = LineRegister()
        port = int(os.getenv("LINE_WEBHOOK_PORT", "5001"))
        debug = os.getenv("FLASK_DEBUG", "1").strip() in {"1", "true", "True"}
        webhook_app.run(port=port, debug=debug)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)