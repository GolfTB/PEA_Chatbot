import argparse
import os
import sqlite3
import threading
import time
import pandas as pd

import timesheet


def _normalize_sheet_date(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "/" in raw:
        parts = raw.split("/")
        if len(parts) == 3:
            return "".join(parts)
    return raw


def _sheet_date_to_db(value: str) -> str:
    if not value or len(value) != 6 or not value.isdigit():
        return ""
    return f"{value[:2]}/{value[2:4]}/{value[4:6]}"


def generate_report_image(sheet_date: str):
    date_str = _sheet_date_to_db(sheet_date)
    if not date_str:
        raise ValueError("sheet_date ต้องเป็นรูปแบบ DDMMYY หรือ DD/MM/YY")

    conn = sqlite3.connect(timesheet.DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM attendance WHERE stampdate = ?",
        conn,
        params=(date_str,),
    )
    conn.close()

    if df.empty:
        raise ValueError(f"ไม่พบข้อมูลวันที่ระบุ: {date_str}")

    detail_df, summary_data, missing_df = timesheet.prepare_data(df, report_date_str=date_str)

    day = sheet_date[:2]
    month = sheet_date[2:4]
    year = sheet_date[4:6]
    thai_months = [
        '', 'มกราคม', 'กุมภาพันธ์', 'มีนาคม', 'เมษายน',
        'พฤษภาคม', 'มิถุนายน', 'กรกฎาคม', 'สิงหาคม',
        'กันยายน', 'ตุลาคม', 'พฤศจิกายน', 'ธันวาคม'
    ]
    report_date = f"{int(day)} {thai_months[int(month)]} {int('20' + year) + 543}"

    return timesheet.create_report_matplotlib(
        detail_df,
        summary_data,
        report_date,
        missing_df=missing_df,
    )


def main():
    parser = argparse.ArgumentParser(description="Generate report from DB and send to LINE via MQTT")
    parser.add_argument("sheet_date", nargs="?", help="DDMMYY หรือ DD/MM/YY (ถ้าไม่ระบุจะใช้วันล่าสุด)")
    parser.add_argument("--no-send", action="store_true", help="สร้างไฟล์อย่างเดียว ไม่ส่งเข้า LINE")
    args = parser.parse_args()

    sheet_date = _normalize_sheet_date(args.sheet_date) if args.sheet_date else ""
    if not sheet_date:
        sheet_date = timesheet._get_latest_sheet_date()

    if not sheet_date:
        raise SystemExit("ไม่พบวันที่ในฐานข้อมูล")

    sheet_date = _normalize_sheet_date(sheet_date)
    if "/" in sheet_date:
        sheet_date = sheet_date.replace("/", "")

    img_io = generate_report_image(sheet_date)

    timestamp = time.strftime("%Y%m%d%H%M%S")
    filename = f"timesheet_{sheet_date}_{timestamp}.png"
    file_path = os.path.join(timesheet.REPORTS_DIR, filename)
    with open(file_path, "wb") as f:
        f.write(img_io.getbuffer())

    print(f"✅ บันทึกไฟล์รายงาน: {file_path}")

    ttl_seconds = int(os.getenv("TIMESHEET_REPORT_TTL_SECONDS", "600"))

    def _delete_file(path: str, delay: int):
        def _remove():
            try:
                if os.path.exists(path):
                    os.remove(path)
                    print(f"🧹 ลบไฟล์รายงานชั่วคราวแล้ว: {path}")
            except Exception as e:
                print(f"⚠️ ลบไฟล์รายงานไม่สำเร็จ: {e}")
        t = threading.Timer(delay, _remove)
        t.daemon = True
        t.start()

    if args.no_send:
        _delete_file(file_path, ttl_seconds)
        return

    base_url = os.getenv("TIMESHEET_REPORTS_BASE_URL", "").strip()
    if not base_url:
        base_url = os.getenv("TIMESHEET_PLOT_BASE_URL", "").strip()
    base_url = base_url.rstrip("/")

    if not base_url:
        raise SystemExit("ต้องตั้ง TIMESHEET_REPORTS_BASE_URL หรือ TIMESHEET_PLOT_BASE_URL เพื่อส่งรูปเข้า LINE")

    image_url = f"{base_url}/reports/{filename}"

    group_id = os.getenv("LINE_GROUP_ID", "").strip()
    if not group_id:
        raise SystemExit("ไม่พบ LINE_GROUP_ID ใน .env")

    host = os.getenv("MQTT_HOST", "").strip()
    if not host:
        raise SystemExit("ไม่พบ MQTT_HOST ใน .env")

    port = int(os.getenv("MQTT_PORT", "1883"))
    username = os.getenv("MQTT_USERNAME", "").strip()
    password = os.getenv("MQTT_PASSWORD", "").strip()
    topic = os.getenv("MQTT_REPLY_TOPIC", "ai_timesheet_reply").strip()
    to = os.getenv("MQTT_REPLY_TO", "line_webhook").strip()
    source_type = os.getenv("LINE_SOURCE_TYPE", "group").strip() or "group"

    text = f"รายงาน Timesheet ประจำวันที่ {timesheet._format_sheet_date_display(sheet_date)}"

    payload = timesheet._build_mqtt_line_payload(
        to=to,
        source_id=group_id,
        source_type=source_type,
        text=text,
        image_url=image_url,
        preview_image_url=image_url,
    )

    timesheet.mqtt_publish.single(
        topic=topic,
        payload=timesheet.json.dumps(payload, ensure_ascii=False),
        hostname=host,
        port=port,
        auth={"username": username, "password": password} if username else None,
    )

    print(f"✅ ส่งภาพเข้า LINE แล้ว: {image_url}")
    _delete_file(file_path, ttl_seconds)


if __name__ == "__main__":
    main()
