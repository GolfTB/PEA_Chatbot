import os
import sys
import json
import time
import threading
import requests
from dotenv import load_dotenv

base_dir = os.path.dirname(os.path.abspath(__file__))
mqtt_dir = os.path.dirname(base_dir)
project_root = os.path.dirname(mqtt_dir)

for path in (mqtt_dir, project_root):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent import Agent

try:
    import basic_agent_langchain_tool as attendance_ai
except Exception:
    attendance_ai = None


workername = (os.getenv("MQTT_WORKER_NAME", "ai_timesheet") or "ai_timesheet").strip().strip(",")
operator = (os.getenv("MQTT_OPERATOR", "ai_timesheet") or "ai_timesheet").strip().strip(",")
inbound_topic = (os.getenv("MQTT_INBOUND_TOPIC", "ai_timesheet") or "ai_timesheet").strip().strip(",")
reply_topic = (os.getenv("MQTT_REPLY_TOPIC", "ai_timesheet_reply") or "ai_timesheet_reply").strip().strip(",")
reply_to = (os.getenv("MQTT_REPLY_TO", "line_webhook") or "line_webhook").strip().strip(",")
reply_via_mqtt = os.getenv("ATTENDANCE_REPLY_VIA_MQTT", "1").strip().lower() in {"1", "true", "yes", "y"}

agent = None
MQTT_HOST = None
MQTT_USERNAME = None
MQTT_PASSWORD = None


def linepost(reply_token: str, msg: str) -> None:
    line_access_token = os.getenv("LINE_ACCESS_TOKEN") or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    line_reply_url = os.getenv("LINE_URL", "https://api.line.me/v2/bot/message/reply")
    if not line_access_token:
        print("[ERROR] LINE_ACCESS_TOKEN / LINE_CHANNEL_ACCESS_TOKEN is not set")
        return

    if not reply_token:
        print("[WARNING] Missing reply token, skip LINE reply")
        return

    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": msg}],
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {line_access_token}",
    }

    try:
        response = requests.post(line_reply_url, headers=headers, data=json.dumps(payload), timeout=20)
        if response.status_code >= 400:
            print(f"[ERROR] LINE reply failed: {response.status_code} {response.text}")
    except Exception as exc:
        print(f"[ERROR] LINE reply exception: {exc}")


def post_response(
    reply_token: str,
    msg: str,
    employee_id: str = "UNKNOWN",
    line_uuid: str = "",
    source_type: str = "",
    source_id: str = "",
) -> None:
    token_prefix = f"{reply_token[:8]}..." if reply_token else "(empty)"
    print(
        f"[RETURN] prepare reply token={token_prefix} employee_id={employee_id} "
        f"mode={'MQTT' if reply_via_mqtt else 'LINE'}"
    )

    if reply_via_mqtt:
        if not agent:
            print("[WARNING] MQTT agent not ready, fallback to direct LINE reply")
            linepost(reply_token, msg)
            return

        payload = {
            "frm": workername,
            "to": reply_to,
            "topic": "attendance_result",
            "contents": {
                "msg": {
                    "rep": reply_token,
                    "res": msg,
                    "employee_id": employee_id,
                    "line_uuid": line_uuid,
                    "source_type": source_type,
                    "source_id": source_id,
                }
            },
        }

        try:
            print(
                f"[RETURN] publish MQTT topic={reply_topic} to={reply_to} "
                f"payload_topic=attendance_result token={token_prefix}"
            )
            agent.pub(reply_topic, json.dumps(payload, ensure_ascii=False))
            print(
                f"[MQTT] reply published topic={reply_topic} to={reply_to} "
                f"employee_id={employee_id} token={token_prefix}"
            )
            return
        except Exception as exc:
            print(f"[ERROR] MQTT reply publish failed: {exc}; fallback to direct LINE reply")
            linepost(reply_token, msg)
            return

    linepost(reply_token, msg)


def publish_status(topic: str, title: str, msg: str) -> None:
    if not agent:
        return

    payload = {
        "frm": workername,
        "to": "control",
        "topic": topic,
        "content": {
            "title": title,
            "msg": msg,
        },
    }

    try:
        agent.pub("app", json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        print(f"[WARNING] publish_status failed: {exc}")


def on_message(client, userdata, message):
    raw_topic = message.topic
    raw_payload = message.payload.decode("utf-8", "strict")

    should_process = False

    try:
        parsed = json.loads(raw_payload)
        target = str(parsed.get("to", "")).strip().strip(",")

        if target == operator:
            should_process = True
        elif not target and raw_topic == inbound_topic:
            should_process = True
            print("[ROUTING] fallback by topic because payload has no 'to'")
    except Exception:
        if raw_topic == inbound_topic:
            should_process = True
            print("[ROUTING] fallback by topic because payload is not JSON")

    if should_process:
        threadhook = threading.Thread(target=gethooked, args=(raw_payload,), daemon=True)
        threadhook.start()


def gethooked(raw_payload: str):
    started = time.time()

    try:
        payload = json.loads(raw_payload)
    except Exception:
        print("[ERROR] Invalid JSON payload")
        return False

    topic = payload.get("topic", "")
    if topic and topic != "llm":
        print(f"[INFO] Skip unsupported topic: {topic}")
        return False

    contents = payload.get("contents", {})
    msg_content = contents.get("msg", {})

    if isinstance(msg_content, dict):
        reply_token = str(msg_content.get("rep", "")).strip()
        user_text = str(msg_content.get("res", "")).strip()
        employee_id = str(msg_content.get("employee_id", "UNKNOWN")).strip() or "UNKNOWN"
        line_uuid = str(msg_content.get("line_uuid", "")).strip()
        source_type = str(msg_content.get("source_type", "")).strip().lower()
        source_id = str(msg_content.get("source_id", "")).strip()
    else:
        reply_token = ""
        user_text = str(msg_content).strip()
        employee_id = "UNKNOWN"
        line_uuid = ""
        source_type = ""
        source_id = ""

    if not user_text:
        print("[WARNING] Empty user message")
        return False

    if attendance_ai is None:
        err = "ระบบ Attendance AI ยังไม่พร้อมใช้งาน (ไม่พบ basic_agent_langchain_tool runtime)"
        print(f"[ERROR] {err}")
        post_response(
            reply_token,
            err,
            employee_id=employee_id,
            line_uuid=line_uuid,
            source_type=source_type,
            source_id=source_id,
        )
        return False

    try:
        if hasattr(attendance_ai, "process_line_message"):
            result = attendance_ai.process_line_message(
                res=user_text,
                rep=reply_token,
                employee_id=employee_id,
            )
            response_text = str(result.get("response_text", "บันทึกคำขอเรียบร้อยแล้ว"))
        else:
            payload_json = attendance_ai.ask_attendance_json(user_text=user_text)
            persisted = attendance_ai.persist_attendance_payload(
                payload_json=payload_json,
                employee_id=employee_id,
            )

            dates = ", ".join(persisted["dates"])
            response_text = (
                f"บันทึกคำขอเรียบร้อยแล้ว\n"
                f"พนักงาน: {employee_id}\n"
                f"ประเภท: {persisted['category']}\n"
                f"วันที่: {dates}\n"
                f"เหตุผล: {persisted['reason']}\n"
                # f"จำนวนรายการที่บันทึก: {persisted['inserted']}"
            )
        post_response(
            reply_token,
            response_text,
            employee_id=employee_id,
            line_uuid=line_uuid,
            source_type=source_type,
            source_id=source_id,
        )

        elapsed = time.time() - started
        print(f"[OK] attendance processed in {elapsed:.2f}s employee_id={employee_id}")
        return True
    except Exception as exc:
        print(f"[ERROR] attendance processing failed: {exc}")
        post_response(
            reply_token,
            "ขออภัยค่ะ ระบบไม่สามารถบันทึกคำขอได้ในขณะนี้ กรุณาลองใหม่อีกครั้ง",
            employee_id=employee_id,
            line_uuid=line_uuid,
            source_type=source_type,
            source_id=source_id,
        )
        return False


def main():
    global agent, MQTT_HOST, MQTT_USERNAME, MQTT_PASSWORD, operator, inbound_topic, workername
    global reply_topic, reply_to, reply_via_mqtt

    load_dotenv(os.path.join(base_dir, ".env"))

    workername = (os.getenv("MQTT_WORKER_NAME", workername) or workername).strip().strip(",")
    operator = (os.getenv("MQTT_OPERATOR", operator) or operator).strip().strip(",")
    inbound_topic = (os.getenv("MQTT_INBOUND_TOPIC", inbound_topic) or inbound_topic).strip().strip(",")
    reply_topic = (os.getenv("MQTT_REPLY_TOPIC", reply_topic) or reply_topic).strip().strip(",")
    reply_to = (os.getenv("MQTT_REPLY_TO", reply_to) or reply_to).strip().strip(",")
    reply_via_mqtt = os.getenv("ATTENDANCE_REPLY_VIA_MQTT", "1").strip().lower() in {"1", "true", "yes", "y"}

    MQTT_HOST = os.getenv("MQTT_HOST")
    MQTT_USERNAME = os.getenv("MQTT_USERNAME")
    MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

    print("=" * 60)
    print("📌 PEA Attendance AI Worker")
    print("=" * 60)
    print(f"Broker host={MQTT_HOST}, username={'set' if MQTT_USERNAME else 'empty'}")
    print(f"Routing operator={operator}, inbound_topic={inbound_topic}")
    print(f"Reply mode={'MQTT' if reply_via_mqtt else 'LINE direct'}, reply_topic={reply_topic}, reply_to={reply_to}")

    if not MQTT_HOST:
        print("❌ ERROR: MQTT_HOST environment variable is not set.")
        return

    if attendance_ai is None:
        print("❌ ERROR: ไม่พบโมดูล basic_agent_langchain_tool")
        return

    try:
        attendance_ai.load_env()
        attendance_ai.ensure_db()
        print("✅ Attendance runtime ready")
    except Exception as exc:
        print(f"❌ ERROR: init attendance runtime failed: {exc}")
        return

    agent = Agent(topic="listener", host=MQTT_HOST, username=MQTT_USERNAME, password=MQTT_PASSWORD)

    try:
        thread = threading.Thread(target=agent.online, args=(on_message,), daemon=True)
        thread.start()

        time.sleep(2)
        publish_status("info", "online", f"{workername} online")

        print("✅ Attendance worker is online and waiting for MQTT messages...")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        publish_status("info", "offline", f"{workername} offline")
        print("\n👋 ปิดระบบแล้ว")


if __name__ == "__main__":
    main()