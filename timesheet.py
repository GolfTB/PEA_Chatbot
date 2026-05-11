from flask import Flask, send_file, jsonify, request
import sqlite3
import pandas as pd
from pythainlp.util import display_thai_char
import matplotlib.pyplot as plt
from matplotlib import font_manager
import io
import matplotlib
matplotlib.use('Agg')
from datetime import datetime
import requests
import time
import threading
import os
import json
import re
from dotenv import load_dotenv
import paho.mqtt.publish as mqtt_publish

app = Flask(__name__)
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'attendance.db')
REPORTS_DIR = os.path.join(BASE_DIR, 'reports')
os.makedirs(REPORTS_DIR, exist_ok=True)
_SEND_GUARD_LOCK = threading.Lock()
_INFLIGHT_SEND_SHEETS = set()

# ==========================================
# การตั้งค่า Geocoding
# ==========================================
USE_GOOGLE_MAPS = False
GOOGLE_API_KEY = ""

# ==========================================
# ฟังก์ชันจัดการ Database
# ==========================================
def init_database():
    """สร้าง table attendance และเพิ่มคอลัมน์ location_name, location_updated ถ้ายังไม่มี"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)  # เพิ่ม timeout 30 วินาที
        cursor = conn.cursor()

        # สร้าง table ถ้ายังไม่มี
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                objectid     INTEGER PRIMARY KEY,
                peauser      TEXT,
                pullfullname TEXT,
                pullposition TEXT,
                pulldept     TEXT,
                checktime    TEXT,
                stamptype    TEXT,
                stampdate    TEXT,
                oper         TEXT,
                commenttext  TEXT,
                y            REAL DEFAULT 0,
                x            REAL DEFAULT 0,
                location_name    TEXT,
                location_updated TEXT
            )
        """)

        # ตรวจสอบว่ามีคอลัมน์แล้วหรือยัง (รองรับ database เก่า)
        cursor.execute("PRAGMA table_info(attendance)")
        columns = [col[1] for col in cursor.fetchall()]
        
        # เพิ่มคอลัมน์ถ้ายังไม่มี
        if 'location_name' not in columns:
            cursor.execute("ALTER TABLE attendance ADD COLUMN location_name TEXT")
            print("✅ เพิ่มคอลัมน์ location_name")
        
        if 'location_updated' not in columns:
            cursor.execute("ALTER TABLE attendance ADD COLUMN location_updated TEXT")
            print("✅ เพิ่มคอลัมน์ location_updated")
        
        conn.commit()
        conn.close()
        print("✅ Database พร้อมใช้งาน")
        
    except Exception as e:
        print(f"❌ Error init database: {e}")

# เรียกใช้ตอนเริ่มต้น
init_database()

# ==========================================
# ฟังก์ชันแปลงพิกัดเป็นสถานที่
# ==========================================
def get_location_name_nominatim(latitude, longitude):
    """แปลงพิกัดเป็นชื่อสถานที่ด้วย OpenStreetMap"""
    try:
        if latitude <= 1.0 or longitude <= 1.0:
            return None

        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            "lat": latitude,
            "lon": longitude,
            "format": "json",
            "accept-language": "th"
        }
        headers = {"User-Agent": "AttendanceReport/1.0"}

        response = requests.get(url, params=params, headers=headers, timeout=10)  # เพิ่ม timeout 10 วินาที
        data = response.json()

        if "display_name" in data:
            parts = data["display_name"].split(",")
            return parts[0].strip()
        return None
    except:
        return None

def get_location_name_google(latitude, longitude, api_key):
    """แปลงพิกัดเป็นชื่อสถานที่ด้วย Google Maps"""
    try:
        if latitude <= 1.0 or longitude <= 1.0:
            return None

        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            "latlng": f"{latitude},{longitude}",
            "key": api_key,
            "language": "th"
        }

        response = requests.get(url, params=params, timeout=10)  # เพิ่ม timeout 10 วินาที
        data = response.json()

        if data["status"] == "OK" and len(data["results"]) > 0:
            address = data["results"][0]["formatted_address"]
            parts = address.split(",")
            return parts[0].strip()
        return None
    except:
        return None

# ==========================================
# ฟังก์ชัน Auto-Resolve (Background Thread)
# ==========================================
def auto_resolve_location(objectid, y, x):
    """
    แปลงพิกัดเป็นชื่อสถานที่โดยอัตโนมัติในพื้นหลัง
    ทำงานผ่าน Background Thread เพื่อไม่ให้ check-in ช้า
    จะ skip ถ้าสถานที่ถูกตั้งไว้แล้ว (manual rename)
    """
    try:
        if USE_GOOGLE_MAPS and GOOGLE_API_KEY:
            location = get_location_name_google(y, x, GOOGLE_API_KEY)
        else:
            location = get_location_name_nominatim(y, x)

        if location:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            # อัพเดตเฉพาะแถวที่ยังไม่มีชื่อสถานที่ (ไม่ทับชื่อที่ตั้งเองไว้)
            cursor.execute("""
                UPDATE attendance
                SET location_name = ?,
                    location_updated = datetime('now', 'localtime')
                WHERE objectid = ?
                  AND (location_name IS NULL OR location_name = '')
            """, (location, objectid))
            conn.commit()
            conn.close()
            print(f"  📍 Auto-resolved: objectid={objectid} → {location}")
        else:
            print(f"  ⚠️ ไม่พบชื่อสถานที่: objectid={objectid} ({y}, {x})")
    except Exception as e:
        print(f"  ❌ Error auto_resolve_location objectid={objectid}: {e}")


# ==========================================
# ฟังก์ชัน update locations แบบ Standalone (ใช้ได้โดยไม่ต้องรัน server)
# ==========================================
def update_all_locations(dbname: str = DB_PATH) -> dict:
    """
    อัพเดตพิกัดทั้งหมดที่ยังไม่มี location_name เป็นชื่อสถานที่
    ใช้งานได้โดยตรงโดยไม่ต้องรัน Flask server

    Args:
        dbname: ชื่อไฟล์ database (default: DB_PATH)

    Returns:
        dict: { 'total': int, 'updated': int, 'failed': int }
    """
    try:
        conn   = sqlite3.connect(dbname)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT objectid, y, x
            FROM attendance
            WHERE (location_name IS NULL OR location_name = '')
              AND y > 1.0 AND x > 1.0
        """)
        records = cursor.fetchall()
        conn.close()

        if not records:
            print("ℹ️  update_all_locations: ไม่พบข้อมูลที่ต้องอัพเดต")
            return {'total': 0, 'updated': 0, 'failed': 0}

        print(f"📍 กำลังอัพเดตพิกัด {len(records)} รายการ...")
        updated, failed = 0, 0

        for objectid, y, x in records:
            try:
                if USE_GOOGLE_MAPS and GOOGLE_API_KEY:
                    location = get_location_name_google(y, x, GOOGLE_API_KEY)
                else:
                    location = get_location_name_nominatim(y, x)
                    time.sleep(1.1)  # Rate limit

                if location:
                    conn   = sqlite3.connect(dbname)
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE attendance
                        SET location_name = ?,
                            location_updated = datetime('now', 'localtime')
                        WHERE objectid = ?
                    """, (location, objectid))
                    conn.commit()
                    conn.close()
                    print(f"  ✅ {objectid}: {location}")
                    updated += 1
                else:
                    print(f"  ⚠️ {objectid}: ไม่พบชื่อสถานที่")
                    failed += 1
            except Exception as e:
                print(f"  ❌ {objectid}: {e}")
                failed += 1

        print(f"✅ อัพเดตเสร็จสิ้น: {updated} สำเร็จ, {failed} ล้มเหลว")
        return {'total': len(records), 'updated': updated, 'failed': failed}

    except Exception as e:
        print(f"❌ update_all_locations error: {e}")
        return {'total': 0, 'updated': 0, 'failed': 0}


# ==========================================
# API 1: อัพเดตพิกัดเป็นชื่อสถานที่
# ==========================================
@app.route('/update_locations', methods=['POST'])
def update_locations():
    """
    อัพเดตพิกัดเป็นชื่อสถานที่ใน Database
    
    Request Body (JSON):
    {
        "objectids": [1324, 1328, 1349],  // optional - ระบุ objectid ที่ต้องการอัพเดต
        "all": true                        // optional - อัพเดตทั้งหมด
    }
    
    Returns:
        JSON: สถานะการอัพเดต
    """
    try:
        data = request.get_json() or {}
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # กำหนดเงื่อนไข WHERE
        if data.get('all'):
            # อัพเดตทั้งหมดที่ยังไม่มี location_name
            query = """
                SELECT objectid, y, x 
                FROM attendance 
                WHERE (location_name IS NULL OR location_name = '')
                AND y > 1.0 AND x > 1.0
            """
            cursor.execute(query)
        elif 'objectids' in data:
            # อัพเดตเฉพาะ objectid ที่ระบุ
            objectids = data['objectids']
            placeholders = ','.join('?' * len(objectids))
            query = f"""
                SELECT objectid, y, x 
                FROM attendance 
                WHERE objectid IN ({placeholders})
                AND y > 1.0 AND x > 1.0
            """
            cursor.execute(query, objectids)
        else:
            return jsonify({
                "error": "ต้องระบุ 'objectids' หรือ 'all': true"
            }), 400
        
        records = cursor.fetchall()
        
        if not records:
            return jsonify({
                "message": "ไม่พบข้อมูลที่ต้องอัพเดต",
                "updated": 0
            }), 200
        
        print(f"📍 กำลังอัพเดตพิกัด {len(records)} รายการ...")
        
        updated_count = 0
        failed_count = 0
        
        for objectid, y, x in records:
            try:
                # แปลงพิกัดเป็นชื่อสถานที่
                if USE_GOOGLE_MAPS and GOOGLE_API_KEY:
                    location = get_location_name_google(y, x, GOOGLE_API_KEY)
                else:
                    location = get_location_name_nominatim(y, x)
                    time.sleep(1.1)  # Rate limit
                
                if location:
                    # อัพเดตลง Database
                    cursor.execute("""
                        UPDATE attendance 
                        SET location_name = ?, 
                            location_updated = datetime('now', 'localtime')
                        WHERE objectid = ?
                    """, (location, objectid))
                    
                    updated_count += 1
                    print(f"  ✅ {objectid}: {location}")
                else:
                    failed_count += 1
                    print(f"  ⚠️ {objectid}: ไม่พบชื่อสถานที่")
                
            except Exception as e:
                failed_count += 1
                print(f"  ❌ {objectid}: {e}")
        
        conn.commit()
        conn.close()
        
        return jsonify({
            "message": "อัพเดตเสร็จสิ้น",
            "total": len(records),
            "updated": updated_count,
            "failed": failed_count
        }), 200
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500

# ==========================================
# API 2: ตั้งชื่อสถานที่ด้วยตัวเอง (Manual Rename)
# ==========================================
@app.route('/rename_location', methods=['POST'])
def rename_location():
    """
    ตั้งชื่อสถานที่ (location_name) ให้กับ objectid ที่ระบุด้วยตัวเอง
    
    Request Body (JSON) - แบบเดี่ยว:
    {
        "objectid": 1324,
        "location_name": "สำนักงานใหญ่"
    }
    
    Request Body (JSON) - แบบหลายรายการ:
    {
        "updates": [
            {"objectid": 1324, "location_name": "สำนักงานใหญ่"},
            {"objectid": 1328, "location_name": "สาขาลาดพร้าว"}
        ]
    }
    
    Returns:
        JSON: สถานะการอัพเดต
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "ไม่พบข้อมูล JSON"}), 400

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # ── แบบหลายรายการ ──────────────────────────────────
        if 'updates' in data:
            updates = data['updates']
            if not isinstance(updates, list) or len(updates) == 0:
                conn.close()
                return jsonify({"error": "'updates' ต้องเป็น array และมีอย่างน้อย 1 รายการ"}), 400

            success, failed = [], []
            for item in updates:
                oid = item.get('objectid')
                name = item.get('location_name', '').strip()
                if not oid or not name:
                    failed.append({"objectid": oid, "reason": "ข้อมูลไม่ครบ"})
                    continue

                cursor.execute("""
                    UPDATE attendance
                    SET location_name = ?,
                        location_updated = datetime('now', 'localtime')
                    WHERE objectid = ?
                """, (name, oid))

                if cursor.rowcount > 0:
                    success.append({"objectid": oid, "location_name": name})
                    print(f"  ✅ rename {oid} → {name}")
                else:
                    failed.append({"objectid": oid, "reason": "ไม่พบ objectid"})
                    print(f"  ⚠️ ไม่พบ objectid: {oid}")

            conn.commit()
            conn.close()
            return jsonify({
                "message": "อัพเดตชื่อสถานที่เสร็จสิ้น",
                "success": success,
                "failed": failed,
                "total_updated": len(success)
            }), 200

        # ── แบบเดี่ยว ───────────────────────────────────────
        oid = data.get('objectid')
        name = str(data.get('location_name', '')).strip()

        if not oid:
            conn.close()
            return jsonify({"error": "ต้องระบุ 'objectid'"}), 400
        if not name:
            conn.close()
            return jsonify({"error": "ต้องระบุ 'location_name'"}), 400

        cursor.execute("""
            UPDATE attendance
            SET location_name = ?,
                location_updated = datetime('now', 'localtime')
            WHERE objectid = ?
        """, (name, oid))

        if cursor.rowcount == 0:
            conn.close()
            return jsonify({"error": f"ไม่พบ objectid: {oid}"}), 404

        conn.commit()
        conn.close()
        print(f"  ✅ rename objectid {oid} → {name}")
        return jsonify({
            "message": "อัพเดตชื่อสถานที่สำเร็จ",
            "objectid": oid,
            "location_name": name
        }), 200

    except Exception as e:
        print(f"❌ Error rename_location: {e}")
        return jsonify({"error": str(e)}), 500


# ==========================================
# API: รับข้อมูลเช็คอิน + Auto-resolve ชื่อสถานที่
# ==========================================
@app.route('/checkin', methods=['POST'])
def checkin():
    """
    รับข้อมูลเช็คอินและ auto-resolve ชื่อสถานที่ทันทีใน Background

    Request Body (JSON):
    {
        "objectid"     : 1324,          // จำเป็น
        "peauser"      : "U001",
        "pullfullname" : "สมชาย ใจดี",
        "pullposition" : "วิศวกร",
        "checktime"    : "08:30",
        "stampdate"    : "10/03/26",
        "y"            : 13.7563,       // latitude
        "x"            : 100.5018,      // longitude
        "oper"         : "",
        "commenttext"  : ""
    }

    Returns:
        JSON: ผลการบันทึก + สถานะการ resolve ชื่อสถานที่
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "ไม่พบข้อมูล JSON"}), 400

        objectid = data.get('objectid')
        if not objectid:
            return jsonify({"error": "ต้องระบุ 'objectid'"}), 400

        # ดึงค่าพิกัด
        try:
            lat = float(data.get('y', 0))
            lon = float(data.get('x', 0))
        except (TypeError, ValueError):
            lat, lon = 0.0, 0.0

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # ดึงชื่อคอลัมน์ที่มีอยู่จริงใน table
        cursor.execute("PRAGMA table_info(attendance)")
        existing_cols = {row[1] for row in cursor.fetchall()}

        # คอลัมน์ที่รองรับ
        field_map = {
            'objectid'    : data.get('objectid'),
            'peauser'     : data.get('peauser', ''),
            'pullfullname': data.get('pullfullname', ''),
            'pullposition': data.get('pullposition', ''),
            'checktime'   : data.get('checktime', ''),
            'stampdate'   : data.get('stampdate', ''),
            'y'           : lat,
            'x'           : lon,
            'oper'        : data.get('oper', ''),
            'commenttext' : data.get('commenttext', ''),
        }

        # กรองเฉพาะคอลัมน์ที่มีใน table จริง
        insert_fields = {k: v for k, v in field_map.items() if k in existing_cols}

        cols    = ', '.join(insert_fields.keys())
        holders = ', '.join(['?'] * len(insert_fields))
        values  = list(insert_fields.values())

        cursor.execute(
            f"INSERT OR REPLACE INTO attendance ({cols}) VALUES ({holders})",
            values
        )
        conn.commit()
        conn.close()

        print(f"✅ Check-in บันทึกแล้ว: objectid={objectid}")

        # ── Auto-resolve ชื่อสถานที่ใน Background Thread ──────────────
        has_coords = lat > 1.0 and lon > 1.0
        if has_coords:
            t = threading.Thread(
                target=auto_resolve_location,
                args=(objectid, lat, lon),
                daemon=True
            )
            t.start()
            location_status = "กำลัง resolve ชื่อสถานที่ใน background..."
        else:
            location_status = "ไม่มีพิกัด GPS — ข้าม auto-resolve"

        return jsonify({
            "message"        : "บันทึกข้อมูลเช็คอินสำเร็จ",
            "objectid"       : objectid,
            "location_status": location_status
        }), 200

    except Exception as e:
        print(f"❌ Error checkin: {e}")
        return jsonify({"error": str(e)}), 500


# ==========================================
# ฟังก์ชันเตรียมข้อมูล
# ==========================================

def prepare_data(df, report_date_str=None):
    live_geocode_in_report = os.getenv("TIMESHEET_LIVE_GEOCODE_IN_REPORT", "0").strip() in {"1", "true", "True"}

    # 1. จัดการวันที่
    if report_date_str is None:
        if not df.empty and 'checktime' in df.columns:
            report_date_str = datetime.now().strftime('%Y-%m-%d')
        elif not df.empty and 'timestamp' in df.columns:
            try:
                report_date_str = pd.to_datetime(df['timestamp'].iloc[0]).strftime('%Y-%m-%d')
            except:
                report_date_str = datetime.now().strftime('%Y-%m-%d')
        else:
            report_date_str = datetime.now().strftime('%Y-%m-%d')

    # 2. โหลดข้อมูลพนักงานและสถานะการลาจาก Database (SQLite)
    try:
        conn = sqlite3.connect(DB_PATH)
        employees_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'employees.db')
        emp_conn = sqlite3.connect(employees_db_path)
        
        # ดึงรายชื่อพนักงานทั้งหมด
        try:
            all_emp_df = pd.read_sql_query(
                "SELECT user_id as employee_id, fullname as name, position, dept as department "
                "FROM employees WHERE enabled != 'n' OR enabled IS NULL",
                emp_conn
            )
            all_emp_df['employee_id'] = all_emp_df['employee_id'].astype(str)
            all_emp_df = all_emp_df.drop_duplicates(subset=['employee_id'], keep='first')
        except:
            # Fallback ไปหา CSV ถ้าระบบยังไม่ได้ Migrate db อย่างสมบูรณ์
            emp_csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'employees.csv')
            all_emp_df = pd.read_csv(emp_csv_path)
            all_emp_df['user_id'] = all_emp_df['user_id'].astype(str)
            all_emp_df = all_emp_df.rename(columns={'user_id': 'employee_id', 'fullname': 'name', 'dept': 'department'})
            all_emp_df = all_emp_df.drop_duplicates(subset=['employee_id'], keep='first')
            
# ดึงประวัติการลา/ไปราชการ/WFH จาก commenttext ที่ไม่เป็น NULL ในตาราง attendance
        try:
            # ลองดึงจาก missing_attendance ก่อน (ถ้ามี)
            query1 = f"""
                SELECT peauser as employee_id, status, '' as db_remark
                FROM missing_attendance
                WHERE stampdate = '{report_date_str}'
                AND status != 'ยังไม่ลงเวลา'
            """
            emp_status_df = pd.read_sql_query(query1, conn)
            
            # ถ้าไม่มีข้อมูล ให้ดึงจาก attendance โดยดูจาก commenttext
            if emp_status_df.empty:
                query2 = f"""
                    SELECT peauser as employee_id,
                           CASE 
                               WHEN LOWER(commenttext) LIKE '%ลา%' THEN 'ลา'
                               WHEN LOWER(commenttext) LIKE '%ราชการ%' THEN 'ราชการ'
                               WHEN LOWER(commenttext) LIKE '%wfh%' OR LOWER(commenttext) LIKE '%บ้าน%' THEN 'ปฏิบัติงานที่บ้าน'
                               ELSE commenttext
                           END as status,
                           commenttext as db_remark
                    FROM attendance
                    WHERE stampdate = '{report_date_str}'
                    AND commenttext IS NOT NULL
                    AND commenttext != ''
                """
                emp_status_df = pd.read_sql_query(query2, conn)
            
            emp_status_df['employee_id'] = emp_status_df['employee_id'].astype(str)
            emp_status_df = emp_status_df.drop_duplicates(subset=['employee_id'], keep='last')
        except:
            emp_status_df = pd.DataFrame(columns=['employee_id', 'status', 'db_remark'])
            
        conn.close()
        emp_conn.close()
    except Exception as e:
        print(f"Error loading employee database: {e}")
        emp_status_df = pd.DataFrame(columns=['employee_id', 'status', 'db_remark'])
        all_emp_df = pd.DataFrame(columns=['employee_id', 'name', 'position', 'department'])

    # แปลงวันที่สำหรับการแสดงผล
    if report_date_str:
        try:
            if '/' in report_date_str:
                day, month, year = report_date_str.split('/')
                year_full = int('20' + year)
                date_obj = datetime(year_full, int(month), int(day))
            else:
                date_obj = datetime.strptime(report_date_str, '%Y-%m-%d')
            thai_months = ['ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.', 'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.']
            formatted_date = f"{date_obj.day} {thai_months[date_obj.month-1]} {date_obj.year + 543}"
        except:
            formatted_date = report_date_str
    else:
        formatted_date = "-"

    # ฟังก์ชันแปลงสถานที่
    location_cache = {}

    def format_location(row):
        location_name = row.get('location_name')
        if pd.notna(location_name) and str(location_name).strip() and str(location_name) != 'None':
            return str(location_name)

        # โหมดเร็ว: ใช้เฉพาะ location_name ที่มีอยู่ใน DB
        # ถ้าต้องการให้รายงานไป reverse geocode แบบสด ให้ตั้ง TIMESHEET_LIVE_GEOCODE_IN_REPORT=1
        if not live_geocode_in_report:
            return "-"

        try:
            y = row.get('y')
            x = row.get('x')
            if pd.notna(y) and pd.notna(x) and float(y) > 1.0 and float(x) > 1.0:
                cache_key = (round(float(y), 5), round(float(x), 5))
                if cache_key not in location_cache:
                    if USE_GOOGLE_MAPS and GOOGLE_API_KEY:
                        location_cache[cache_key] = get_location_name_google(float(y), float(x), GOOGLE_API_KEY)
                    else:
                        location_cache[cache_key] = get_location_name_nominatim(float(y), float(x))
                if location_cache[cache_key]:
                    return str(location_cache[cache_key])
        except Exception:
            pass

        return "-"

    # เตรียมข้อมูลจาก df
    if not df.empty:
        emp_col = 'peauser' if 'peauser' in df.columns else 'employee_id'
        time_col = 'checktime' if 'checktime' in df.columns else 'timestamp'
        remark_col = 'commenttext' if 'commenttext' in df.columns else 'remark'
        
        time_df = df.copy()
        if emp_col in time_df.columns: time_df['employee_id'] = time_df[emp_col]
        if time_col in time_df.columns: time_df['portal_time'] = time_df[time_col]
        else: time_df['portal_time'] = '-'
        
        if remark_col in time_df.columns: time_df['portal_remark'] = time_df[remark_col]
        else: time_df['portal_remark'] = '-'
        
        time_df['location'] = time_df.apply(format_location, axis=1)
        time_df['time'] = time_df['portal_time'].astype(str).str.replace('.', ':', regex=False)
        time_df['oper'] = df['oper'].fillna('-') if 'oper' in df.columns else '-'
        time_df = time_df.sort_values('time').drop_duplicates('employee_id', keep='last')
        
        time_df['remark'] = time_df['portal_remark'].fillna('-')
        time_df['remark'] = time_df['remark'].apply(lambda x: '-' if str(x).strip() in ['None', 'nan', ''] else str(x).strip())
    else:
        time_df = pd.DataFrame(columns=['employee_id', 'time', 'location', 'remark', 'oper'])

    # ประกอบร่าง All Employees + time_df + emp_status_df
    merged = all_emp_df.merge(time_df[['employee_id', 'time', 'location', 'remark', 'oper']], on='employee_id', how='left')
    merged = merged.merge(emp_status_df, on='employee_id', how='left')

    def derive_final_status(row):
        s = str(row['status']).strip() if pd.notna(row['status']) else ""
        r = str(row['remark']).strip() if pd.notna(row['remark']) else ""
        o = str(row.get('oper', '')).strip() if pd.notna(row.get('oper', '')) else ""
        
        # ตรวจสอบหมายเหตุก่อนแบบเจาะจง (ราชการ, ลา, wfh)
        combined = (s + " " + r + " " + str(row.get('db_remark', '')) + " " + o).lower()
        
        if 'ราชการ' in combined:
            return 'ราชการ'
        elif 'ลา' in combined or 'leave' in combined: # รวบรวม ลาป่วย, ลาเช้า, ลาครึ่งวัน ฯลฯ
            return 'ลา'
        elif 'work from home' in combined or 'wfh' in combined or s.lower() == 'work from home':
            return 'ปฏิบัติงานที่บ้าน'
        elif pd.notna(row['time']) and str(row['time']) != '-' and str(row['time']).strip() != "nan":
            return 'ปฏิบัติงาน ณ สำนักงาน'
        elif 'ออฟฟิศ' in s or 'สำนักงาน' in s or 'ปฏิบัติงาน ณ สำนักงาน' in s:
            return 'ปฏิบัติงาน ณ สำนักงาน'
        
        return 'ไม่ได้ลงเวลา'

    merged['final_status'] = merged.apply(derive_final_status, axis=1)
    
    merged['location'] = merged['location'].fillna('-')
    merged['time'] = merged['time'].fillna('-').astype(str)
    merged['time'] = merged['time'].replace('nan', '-')
    merged['remark'] = merged['remark'].fillna('-')
    
    def derive_final_remark(row):
        s = str(row['status']).strip() if pd.notna(row['status']) else ""
        dbr = str(row.get('db_remark', '')).strip()
        r = str(row['remark']).strip()
        
        final_r = ""
        if r != '-' and r != '':
            final_r = r
            
        # ถ้ามีสถานะจากไลน์ (การลาต่างๆ) ให้นำมาเก็บไว้ในหมายเหตุ
        remark_parts = []
        if final_r != "":
            remark_parts.append(final_r)
        if s != '' and s not in ['ออฟฟิศ', 'สำนักงาน', 'ปฏิบัติงาน ณ สำนักงาน', 'Work from home', 'ไม่ได้ลงเวลา', 'pending', 'approved']:
            if s not in final_r:
                remark_parts.append(s)
        if dbr != '' and str(dbr) != 'nan':
            # มีเหตุผลแนบมาด้วย
            remark_parts.append(f"({dbr})")
            
        final_rem = " ".join(remark_parts).strip()
        return final_rem if final_rem != "" else "-"

    merged['final_remark'] = merged.apply(derive_final_remark, axis=1)

    did_action = merged[merged['final_status'] != 'ไม่ได้ลงเวลา'].copy()
    no_action = merged[merged['final_status'] == 'ไม่ได้ลงเวลา'].copy()

    total_office = len(did_action[did_action['final_status'] == 'ปฏิบัติงาน ณ สำนักงาน'])
    total_wfh = len(did_action[did_action['final_status'] == 'ปฏิบัติงานที่บ้าน'])
    total_official = len(did_action[did_action['final_status'] == 'ราชการ'])
    total_leave = len(did_action[did_action['final_status'] == 'ลา'])
    total_missing = len(no_action)

    total_employees = len(all_emp_df)

    summary_data = [
        ['พนักงานทั้งหมด', f"{total_employees} คน"],
        ['ปฏิบัติงาน ณ สำนักงาน', f"{total_office} คน"],
        ['ราชการ', f"{total_official} คน"],
        ['ลา', f"{total_leave} คน"],
        ['ปฏิบัติงานที่บ้าน', f"{total_wfh} คน"],
        ['ไม่ได้ลงเวลา', f"{total_missing} คน"]
    ]

    target_columns = ['ลำดับ', 'รหัส', 'ชื่อ-นามสกุล', 'ตำแหน่ง', 'เวลา', 'พิกัด (Y, X)', 'หมายเหตุ']

    if not did_action.empty:
        def get_order(st):
            if st == 'ปฏิบัติงาน ณ สำนักงาน': return 1
            if st == 'ปฏิบัติงานที่บ้าน': return 2
            if st == 'ราชการ': return 3
            if st == 'ลา': return 4
            return 5
        did_action['order'] = did_action['final_status'].apply(get_order)
        did_action = did_action.sort_values(['order', 'time'])
        did_action['ลำดับ'] = range(1, len(did_action) + 1)
        detail_df_final = did_action[['ลำดับ', 'employee_id', 'name', 'position', 'time', 'location', 'final_remark']]
        detail_df_final.columns = target_columns
    else:
        detail_df_final = pd.DataFrame(columns=target_columns)

    missing_columns = ['ลำดับ', 'รหัส', 'ชื่อ-นามสกุล', 'ตำแหน่ง']
    if not no_action.empty:
        no_action = no_action.sort_values('employee_id')
        no_action['ลำดับ'] = range(1, len(no_action) + 1)
        missing_df_final = no_action[['ลำดับ', 'employee_id', 'name', 'position']]
        missing_df_final.columns = missing_columns
    else:
        missing_df_final = pd.DataFrame(columns=missing_columns)

    return detail_df_final, summary_data, missing_df_final
def setup_fonts():
    # --- ฟอนต์ไทย (ลำดับแรก) ---
    thai_font_paths = [
        '/System/Library/Fonts/Supplemental/Tahoma.ttf',
        os.path.expanduser('~/Library/Fonts/Sarabun-Regular.ttf'),
        'C:\\Windows\\Fonts\\tahoma.ttf',
        '/usr/share/fonts/truetype/thai/Tahoma.ttf',
    ]

    # --- ฟอนต์จีน / CJK (ลำดับสอง) ---
    cjk_font_paths = [
        '/System/Library/Fonts/STHeiti Light.ttc',       # macOS
        '/System/Library/Fonts/STHeiti Medium.ttc',      # macOS
        '/System/Library/Fonts/Hiragino Sans GB.ttc',    # macOS
        'C:\\Windows\\Fonts\\msyh.ttc',                  # Windows — Microsoft YaHei
        'C:\\Windows\\Fonts\\simhei.ttf',                # Windows — SimHei
        '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',         # Linux
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', # Linux
    ]

    # ค้นหาฟอนต์ที่มี
    thai_font = next((p for p in thai_font_paths if os.path.exists(p)), None)
    cjk_font  = next((p for p in cjk_font_paths  if os.path.exists(p)), None)

    # ลงทะเบียนฟอนต์กับ matplotlib และดึงชื่อ family
    family_list = []

    if thai_font:
        font_manager.fontManager.addfont(thai_font)
        thai_name = font_manager.FontProperties(fname=thai_font).get_name()
        family_list.append(thai_name)
        print(f"✅ Thai font  : {thai_name}")
    else:
        family_list.append('Sarabun New')

    if cjk_font:
        font_manager.fontManager.addfont(cjk_font)
        cjk_name = font_manager.FontProperties(fname=cjk_font).get_name()
        family_list.append(cjk_name)
        print(f"✅ CJK font   : {cjk_name}")
    else:
        print("⚠️  ไม่พบฟอนต์จีน — ตัวอักษรจีนอาจแสดงเป็น □")

    family_list.append('DejaVu Sans')  # fallback สุดท้าย

    # ตั้งค่า rcParams ให้ matplotlib ใช้ fallback ตามลำดับใน family_list
    matplotlib.rcParams['font.family']     = 'sans-serif'
    matplotlib.rcParams['font.sans-serif'] = family_list

    # ซ่อน warning ปกติ "Glyph X missing from font Tahoma"
    # (ภาษาจีนจะ fallback ไปใช้ Heiti TC อัตโนมัติ)
    import warnings
    warnings.filterwarnings(
        'ignore',
        message=r'Glyph .* missing from font',
        category=UserWarning
    )

    # FontProperties ใช้ 'sans-serif' เพื่อให้ matplotlib วิ่งตาม rcParams fallback list
    # (ถ้าระบุ family='Tahoma' ตรงๆ จะไม่ fallback ไปฟอนต์จีน)
    prop        = font_manager.FontProperties(family='sans-serif', size=13)
    prop_bold   = font_manager.FontProperties(family='sans-serif', size=16, weight='bold')
    prop_header = font_manager.FontProperties(family='sans-serif', size=22, weight='bold')
    prop_title  = font_manager.FontProperties(family='sans-serif', size=16, weight='bold')
    prop_date   = font_manager.FontProperties(family='sans-serif', size=16)

    return prop, prop_bold, prop_header, prop_title, prop_date

# ==========================================
# จัดรูปแบบตาราง
# ==========================================
def style_summary_table(table, prop, prop_bold):
    cells = table.get_celld()
    for (r, c), cell in cells.items():
        cell.set_edgecolor('#888888')
        cell.set_linewidth(0.5)
        cell.PAD = 0.08
        
        if r == 0:
            cell.set_facecolor('#D4D4D4')
            cell.set_text_props(fontproperties=prop_bold, ha='center', va='center')
            cell.PAD = 0.08
        else:
            cell.set_facecolor('#F9F9F9' if r % 2 == 0 else 'white')
            cell.set_text_props(fontproperties=prop, va='center')
            if c == 0:
                cell.set_text_props(ha='left')
                cell.PAD = 0.08
            else:
                cell.set_text_props(ha='center')
                cell.PAD = 0.08

def style_detail_table(table, prop, prop_bold, is_missing=False):
    cells = table.get_celld()
    for (r, c), cell in cells.items():
        cell.PAD = 0.08
    
    # กำหนดคอลัมน์การจัดหน้าจาก Index (อิงตาม Detail โครงสร้างปัจจุบัน: ['ลำดับ', 'รหัส', 'ชื่อ-นามสกุล', 'ตำแหน่ง', 'เวลา', 'พิกัด (Y, X)', 'หมายเหตุ'] )
    # กลุ่มข้อมูลตัวอักษรความยาวไม่คงที่ (ชิดซ้าย) ทั้งหัวตารางและเนื้อหา
    left_align_cols = [2, 3, 5] if not is_missing else [2, 3] # ชื่อ-สกุล, ตำแหน่ง, พิกัด
    # กลุ่มข้อมูลสั้นๆ (กึ่งกลาง) ทั้งหัวตารางและเนื้อหา
    center_align_cols = [0, 1, 4, 6] if not is_missing else [0, 1] # ลำดับ, รหัส, เวลา, หมายเหตุ
    
    for (r, c), cell in cells.items():
        cell.set_edgecolor('#888888')
        cell.set_linewidth(0.5)
        
        # ตั้งค่าการจัดแนวแนวนอนตามกฎ
        if c in left_align_cols:
            ha_align = 'left'
        else:
            ha_align = 'center'
            
        if r == 0:
            cell.set_facecolor('#D4D4D4')
            cell.set_text_props(fontproperties=prop_bold, ha='center', va='center')
            cell.PAD = 0.08
        else:
            cell.set_facecolor('#F9F9F9' if r % 2 == 0 else 'white')
            cell.set_text_props(fontproperties=prop, va='center')
            cell.PAD = 0.08
            cell.set_text_props(ha=ha_align)

def fix_th(s):
    if not isinstance(s, str): return s
    try: return display_thai_char(s)
    except: return s

def fix_df(df):
    try:
        df.columns = [fix_th(c) for c in df.columns]
        for c in df.columns:
            if df[c].dtype == object or df[c].dtype == 'string': df[c] = df[c].apply(fix_th)
    except: pass
    return df

def fix_sum(d):
    try: return [[fix_th(x) for x in r] for r in d]
    except: return d

def create_report_matplotlib(detail_df, summary_data, report_date, missing_df=None, fmt='png'):
    detail_df=fix_df(detail_df)
    summary_data=fix_sum(summary_data)
    if missing_df is not None: missing_df=fix_df(missing_df)

    prop, prop_bold, prop_header, prop_title, prop_date = setup_fonts()
    total_rows = len(detail_df)
    
    FIG_W = 19.0
    ML, MR = 1.50, 1.50
    MT, MB = 1.60, 1.60
    HEAD_H = 1.00
    TITLE_H = 0.36
    GAP1 = 0.32
    GAP2 = 0.50
    GAP_LBL = 0.20
    ROW_H = 0.50
    
    N_SUM = len(summary_data) + 1
    N_DET = total_rows + 1
    SUM_H = N_SUM * ROW_H
    DET_H = N_DET * ROW_H
    
    # Calculate height for missing_df table if it exists
    MISS_H = 0
    if missing_df is not None and not missing_df.empty:
        MISS_H = (len(missing_df) + 1) * ROW_H + GAP2 + TITLE_H + GAP_LBL
    
    fig_h = MT + HEAD_H + GAP1 + TITLE_H + GAP_LBL + SUM_H + GAP2 + TITLE_H + GAP_LBL + DET_H + MISS_H + MB
    
    ax_l = ML / FIG_W
    ax_w = 1.0 - (ML + MR) / FIG_W
    
    def make_ax(y_top_in, h_in, w_frac=None):
        w = w_frac if w_frac is not None else ax_w
        bottom = 1.0 - (y_top_in + h_in) / fig_h
        return fig.add_axes([ax_l, bottom, w, h_in / fig_h])
    
    plt.close('all')
    fig = plt.figure(figsize=(FIG_W, fig_h), facecolor='white', dpi=150)
    
    y = MT
    ax_h = make_ax(y, HEAD_H)
    ax_h.axis('off')
    ax_h.text(0.5, 0.70, "รายงานสรุปการลงเวลาปฏิบัติงานรายบุคคล",
              ha='center', va='center', fontproperties=prop_header, transform=ax_h.transAxes)
    ax_h.text(0.5, 0.22, f"ข้อมูลประจำวันที่ {report_date}",
              ha='center', va='center', fontproperties=prop_date, transform=ax_h.transAxes)
              
    y += HEAD_H + GAP1
    ax_sl = make_ax(y, TITLE_H)
    ax_sl.axis('off')
    ax_sl.text(0.0, 0.5, "1. รายงานสรุปยอดรวม",
               fontproperties=prop_title, transform=ax_sl.transAxes, va='center')
               
    y += TITLE_H + GAP_LBL
    sum_w = ax_w * 0.45
    ax_s = make_ax(y, SUM_H, w_frac=sum_w)
    ax_s.axis('off')
    tbl_sum = ax_s.table(
        cellText=summary_data, colLabels=["รายการ", "จำนวน"],
        cellLoc='left', colWidths=[0.70, 0.30],
        bbox=[0.0, 0.0, 1.0, 1.0], loc='upper left'
    )
    tbl_sum.auto_set_font_size(False)
    tbl_sum.set_fontsize(13)
    style_summary_table(tbl_sum, prop, prop_bold)
    



    y += SUM_H + GAP2
    ax_dl = make_ax(y, TITLE_H)
    ax_dl.axis('off')
    ax_dl.text(0.0, 0.5, "2. รายละเอียดรายบุคคล - กลุ่มที่เข้างาน",
               fontproperties=prop_title, transform=ax_dl.transAxes, va='center')
               
    y += TITLE_H + GAP_LBL
    ax_d = make_ax(y, DET_H)
    ax_d.axis('off')
    if detail_df.empty:
        # Prevent index out of bounds error in matplotlib when DataFrame is empty
        cell_text = [["-" for _ in detail_df.columns]]
    else:
        cell_text = detail_df.values

    tbl_det = ax_d.table(
        cellText=cell_text, colLabels=detail_df.columns,
        cellLoc='left', loc='upper left', bbox=[0.0, 0.0, 1.0, 1.0]
    )
    # Set headers correctly
    tbl_det.auto_set_font_size(False)
    tbl_det.set_fontsize(13)
    tbl_det.auto_set_column_width(col=list(range(len(detail_df.columns))))
    style_detail_table(tbl_det, prop, prop_bold)
    y += DET_H
    
    if missing_df is not None and not missing_df.empty:
        y += GAP2
        ax_ml = make_ax(y, TITLE_H)
        ax_ml.axis('off')
        ax_ml.text(0.0, 0.5, "3. รายละเอียดกลุ่มที่ไม่ได้ลงเวลา",
                   fontproperties=prop_title, transform=ax_ml.transAxes, va='center')
        
        y += TITLE_H + GAP_LBL
        M_DET_H = (len(missing_df) + 1) * ROW_H
        ax_m = make_ax(y, M_DET_H)
        ax_m.axis('off')
        if missing_df.empty:
            miss_cell_text = [["-" for _ in missing_df.columns]]
        else:
            miss_cell_text = missing_df.values

        tbl_miss = ax_m.table(
            cellText=miss_cell_text, colLabels=missing_df.columns,
            cellLoc='left', loc='upper left', bbox=[0.0, 0.0, 1.0, 1.0]
        )
        tbl_miss.auto_set_font_size(False)
        tbl_miss.set_fontsize(13)
        tbl_miss.auto_set_column_width(col=list(range(len(missing_df.columns))))
        style_detail_table(tbl_miss, prop, prop_bold, is_missing=True)
    
    img_io = io.BytesIO()
    plt.savefig(img_io, format=fmt, dpi=150, facecolor='white', edgecolor='none')
    img_io.seek(0)
    plt.close()
    return img_io

# ==========================================
# API 2: สร้างรายงาน
# ==========================================
def _get_latest_sheet_date():
    """ดึงวันที่ล่าสุดที่มีข้อมูลใน Database คืนค่าในรูปแบบ DDMMYY"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT stampdate FROM attendance ORDER BY rowid DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            # stampdate เก็บในรูป 'DD/MM/YY' → แปลงเป็น 'DDMMYY'
            return row[0].replace('/', '')
    except Exception as e:
        print(f"❌ _get_latest_sheet_date error: {e}")
    return None


def _normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip('/')


def _build_report_url(base_url: str, sheet_date: str) -> str:
    base = _normalize_base_url(base_url)
    if not base:
        return ""
    if sheet_date:
        return f"{base}/plotdailylog_image/{sheet_date}"
    return f"{base}/plotdailylog_image"


def _format_sheet_date_display(sheet_date: str) -> str:
    s = (sheet_date or '').strip()
    if len(s) == 6 and s.isdigit():
        return f"{s[:2]}/{s[2:4]}/{s[4:6]}"
    return s


def _build_mqtt_line_payload(
    to: str,
    source_id: str,
    source_type: str,
    text: str,
    image_url: str,
    preview_image_url: str,
):
    message_type = "image" if image_url else "text"
    return {
        "frm": os.getenv("MQTT_FROM", "timesheet"),
        "to": to,
        "topic": "attendance_result",
        "contents": {
            "msg": {
                "rep": "",
                "res": text,
                "employee_id": "SYSTEM",
                "source_type": source_type,
                "source_id": source_id,
                "type": message_type,
                "image_url": image_url,
                "preview_image_url": preview_image_url or image_url,
            }
        },
    }


def _publish_report_to_group(
    sheet_date: str,
    text: str = "",
    group_id: str = "",
    source_type: str = "group",
) -> dict:
    """ส่งรายงานเข้า LINE (group/room/user) ผ่าน MQTT โดยใช้ค่าใน .env"""
    group_id = str(group_id or os.getenv('LINE_GROUP_ID', '')).strip()
    if not group_id:
        return {"ok": False, "error": "ไม่พบ LINE_GROUP_ID"}

    plot_base_url = os.getenv('TIMESHEET_PLOT_BASE_URL', '')
    image_url = _build_report_url(plot_base_url, sheet_date)
    if not image_url:
        return {"ok": False, "error": "ไม่พบ TIMESHEET_PLOT_BASE_URL"}

    host = os.getenv('MQTT_HOST', '').strip()
    if not host:
        return {"ok": False, "error": "ไม่พบ MQTT_HOST"}

    port = int(os.getenv('MQTT_PORT', '1883'))
    username = os.getenv('MQTT_USERNAME', '').strip()
    password = os.getenv('MQTT_PASSWORD', '').strip()
    topic = os.getenv('MQTT_REPLY_TOPIC', 'ai_timesheet_reply').strip()
    to = os.getenv('MQTT_REPLY_TO', 'line_webhook').strip()
    source_type = str(source_type or 'group').strip() or 'group'

    if not text:
        display_date = _format_sheet_date_display(sheet_date)
        text = f"รายงาน Timesheet ประจำวันที่ {display_date}" if display_date else "รายงาน Timesheet ประจำวันที่ ล่าสุด"

    payload = _build_mqtt_line_payload(
        to=to,
        source_id=group_id,
        source_type=source_type,
        text=text,
        image_url=image_url,
        preview_image_url=image_url,
    )

    mqtt_publish.single(
        topic=topic,
        payload=json.dumps(payload, ensure_ascii=False),
        hostname=host,
        port=port,
        auth={"username": username, "password": password} if username else None,
    )
    return {
        "ok": True,
        "group_id": group_id,
        "source_type": source_type,
        "sheet_date": sheet_date,
        "image_url": image_url,
        "topic": topic,
        "to": to,
    }


def _normalize_sheet_date_input(raw: str) -> str:
    """รองรับ DDMMYY หรือ DD/MM/YY หรือ DD-MM-YY แล้วคืนค่า DDMMYY"""
    value = str(raw or '').strip()
    value = value.replace('/', '').replace('-', '')
    return value


def _parse_timesheet_text_command(raw_text: str) -> dict:
    """
    แปลงข้อความคำสั่งสำหรับ LINE mention
    รองรับ:
      - "Timesheet"
      - "Timesheet 210426"
      - "@Bot Timesheet"
      - "@Bot Timesheet 21/04/26"
    """
    text = str(raw_text or '').strip()
    if not text:
        return {"handled": False, "error": "ไม่พบข้อความคำสั่ง"}

    # ตัด mention token นำหน้า (เช่น @TimesheetBot)
    text = re.sub(r'^@\S+\s+', '', text).strip()
    parts = text.split()
    if not parts:
        return {"handled": False, "error": "ไม่พบข้อความคำสั่ง"}

    if parts[0].lower() != 'timesheet':
        return {"handled": False, "error": "ไม่ใช่คำสั่ง Timesheet"}

    if len(parts) == 1:
        return {"handled": True, "use_latest": True, "sheet_date": ""}

    candidate = _normalize_sheet_date_input(parts[1])
    if len(candidate) == 6 and candidate.isdigit():
        return {"handled": True, "use_latest": False, "sheet_date": candidate}

    return {
        "handled": True,
        "use_latest": False,
        "sheet_date": "",
        "error": "รูปแบบวันที่ไม่ถูกต้อง (ใช้ DDMMYY เช่น 210426)",
    }


def _begin_send_once(sheet_date: str):
    key = (sheet_date or '').strip()
    if not key:
        return False, "sheet_date ว่าง"

    with _SEND_GUARD_LOCK:
        if key in _INFLIGHT_SEND_SHEETS:
            return False, "กำลังส่งอยู่"
        _INFLIGHT_SEND_SHEETS.add(key)
        return True, "ok"


def _finish_send_once(sheet_date: str, success: bool):
    key = (sheet_date or '').strip()
    with _SEND_GUARD_LOCK:
        _INFLIGHT_SEND_SHEETS.discard(key)


def _sheet_date_exists(sheet_date: str) -> bool:
    try:
        day = sheet_date[:2]
        month = sheet_date[2:4]
        year = sheet_date[4:6]
        date_str = f"{day}/{month}/{year}"
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM attendance WHERE stampdate = ? LIMIT 1", (date_str,))
        row = cur.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _is_success_result(result) -> bool:
    """ตรวจว่า response/result สำเร็จ (2xx) หรือไม่"""
    if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[1], int):
        return 200 <= result[1] < 300
    status_code = getattr(result, 'status_code', None)
    return isinstance(status_code, int) and 200 <= status_code < 300


@app.route('/plotdailylog')
def plot_timesheet_latest():
    """สร้างรายงานโดยใช้วันที่ล่าสุดที่มีใน Database"""
    latest = _get_latest_sheet_date()
    if not latest:
        return jsonify({"error": "ไม่พบข้อมูลใน Database"}), 404
    print(f"📅 ไม่ระบุวันที่ — ใช้วันที่ล่าสุด: {latest}")

    render_result = _render_plot_timesheet_image(latest)
    if not _is_success_result(render_result):
        print("⚠️ สร้างรายงานไม่สำเร็จ — ข้ามการส่งเข้า LINE")
        return render_result

    # เรียก /plotdailylog แล้วส่งข้อความเป็นวันที่ล่าสุดจริงในตาราง
    try:
        preferred_text = f"รายงาน Timesheet ประจำวันที่ {_format_sheet_date_display(latest)}"

        can_send, reason = _begin_send_once(latest)
        if can_send:
            send_result = _publish_report_to_group(sheet_date=latest, text=preferred_text)
            success = bool(send_result.get("ok"))
            _finish_send_once(latest, success)
            if success:
                print(f"✅ ส่งรายงานเข้า LINE กลุ่มแล้ว: {send_result['group_id']} ({latest})")
            else:
                print(f"⚠️ ข้ามการส่ง LINE กลุ่ม: {send_result.get('error', 'unknown error')}")
        else:
            print(f"ℹ️ ข้ามการส่งซ้ำ: {reason}")
    except Exception as send_err:
        _finish_send_once(latest, False)
        print(f"⚠️ ส่ง LINE กลุ่มไม่สำเร็จ: {send_err}")

    return render_result


def _render_plot_timesheet_image(sheet_date):
    """สร้างภาพรายงานอย่างเดียว (ไม่ส่ง LINE)"""
    day = sheet_date[:2]
    month = sheet_date[2:4]
    year = sheet_date[4:6]
    date_str = f"{day}/{month}/{year}"

    conn = sqlite3.connect(DB_PATH)

    # ดึงข้อมูลพร้อม location_name
    query = f"""
        SELECT * FROM attendance
        WHERE stampdate = '{date_str}'
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    if df.empty:
        return jsonify({
            "error": "ไม่พบข้อมูลวันที่ระบุ",
            "sheet_date": sheet_date
        }), 404

    date_str_iso = df['stampdate'].iloc[0] if not df.empty else None
    detail_df, summary_data, missing_df = prepare_data(df, report_date_str=date_str_iso)

    if detail_df is None:
        return jsonify({
            "error": "ไม่สามารถประมวลผลข้อมูลได้"
        }), 500

    # แปลงวันที่เป็นภาษาไทย
    THAI_MONTHS = [
        '', 'มกราคม', 'กุมภาพันธ์', 'มีนาคม', 'เมษายน',
        'พฤษภาคม', 'มิถุนายน', 'กรกฎาคม', 'สิงหาคม',
        'กันยายน', 'ตุลาคม', 'พฤศจิกายน', 'ธันวาคม'
    ]
    day_int = int(day)
    month_idx = int(month)
    thai_year = int('20' + year) + 543
    report_date = f"{day_int} {THAI_MONTHS[month_idx]} {thai_year}"

    img_io = create_report_matplotlib(detail_df, summary_data, report_date, missing_df=missing_df)

    print(f"✅ สร้างรายงานสำเร็จ: {sheet_date}")

    return send_file(
        img_io,
        mimetype='image/png',
        as_attachment=False,
        download_name=f'timesheet_{sheet_date}.png'
    ), 200


@app.route('/plotdailylog/<sheet_date>')
def plot_timesheet(sheet_date):
    """
    สร้างรายงาน Time Sheet (ดึง location_name จาก Database)
    
    Args:
        sheet_date: รูปแบบ DDMMYY เช่น '170226'
    
    Returns:
        PDF file (streaming)
    """
    try:
        render_result = _render_plot_timesheet_image(sheet_date)
        if not _is_success_result(render_result):
            print("⚠️ สร้างรายงานไม่สำเร็จ — ข้ามการส่งเข้า LINE")
            return render_result

        # ตามที่ต้องการ: เรียก /plotdailylog/<sheet_date> แล้วส่งข้อความเป็น <sheet_date>
        try:
            can_send, reason = _begin_send_once(sheet_date)
            if can_send:
                preferred_text = f"รายงาน Timesheet ประจำวันที่ {_format_sheet_date_display(sheet_date)}"
                publish_result = _publish_report_to_group(sheet_date=sheet_date, text=preferred_text)
                success = bool(publish_result.get("ok"))
                _finish_send_once(sheet_date, success)
                if success:
                    print(f"✅ ส่งรายงานเข้า LINE กลุ่มแล้ว: {publish_result['group_id']} ({sheet_date})")
                else:
                    print(f"⚠️ ข้ามการส่ง LINE กลุ่ม: {publish_result.get('error', 'unknown error')}")
            else:
                print(f"ℹ️ ข้ามการส่งซ้ำ: {reason}")
        except Exception as send_err:
            _finish_send_once(sheet_date, False)
            print(f"⚠️ ส่ง LINE กลุ่มไม่สำเร็จ: {send_err}")

        return render_result

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/plotdailylog_image')
def plot_timesheet_image_latest():
    """สร้างภาพรายงานล่าสุดอย่างเดียว (ไม่ส่ง LINE)"""
    latest = _get_latest_sheet_date()
    if not latest:
        return jsonify({"error": "ไม่พบข้อมูลใน Database"}), 404
    return _render_plot_timesheet_image(latest)


@app.route('/plotdailylog_image/<sheet_date>')
def plot_timesheet_image(sheet_date):
    """สร้างภาพรายงานตามวันที่อย่างเดียว (ไม่ส่ง LINE)"""
    try:
        return _render_plot_timesheet_image(sheet_date)
    except Exception as e:
        print(f"❌ Error plot_timesheet_image: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/reports/<path:filename>')
def serve_report_file(filename):
    """เสิร์ฟไฟล์รายงานที่สร้างไว้ในโฟลเดอร์ reports"""
    base_dir = os.path.abspath(REPORTS_DIR)
    file_path = os.path.abspath(os.path.join(base_dir, filename))
    if not file_path.startswith(base_dir + os.sep):
        return jsonify({"error": "invalid filename"}), 400
    if not os.path.exists(file_path):
        return jsonify({"error": "file not found"}), 404
    return send_file(file_path, mimetype='image/png', as_attachment=False)


@app.route('/send_report_to_group', methods=['POST'])
def send_report_to_group():
    """
    ส่งภาพรายงานเข้า LINE กลุ่มผ่าน MQTT bridge

    Request Body (JSON):
    {
        "sheet_date": "170226",              # optional, ถ้าไม่ส่งจะใช้วันที่ล่าสุด
        "text": "รายงาน Timesheet ประจำวันที่ ...", # optional
        "group_id": "Cxxxx",                 # optional, default จาก LINE_GROUP_ID
        "source_type": "group",              # optional: group|room|user
        "image_url": "https://..."           # optional, ถ้าไม่ส่งจะประกอบจาก TIMESHEET_PLOT_BASE_URL
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        sheet_date = str(data.get('sheet_date', '')).strip()
        if not sheet_date:
            sheet_date = _get_latest_sheet_date() or ""

        if not sheet_date or not _sheet_date_exists(sheet_date):
            return jsonify({
                "error": "ไม่พบข้อมูลวันที่ระบุ",
                "sheet_date": sheet_date or None,
            }), 404

        group_id = str(data.get('group_id') or os.getenv('LINE_GROUP_ID', '')).strip()
        source_type = str(data.get('source_type', 'group')).strip() or 'group'
        text = str(data.get('text', '')).strip()

        if not group_id:
            return jsonify({
                "error": "ไม่พบ LINE group id (ระบุ group_id หรือกำหนด LINE_GROUP_ID ใน .env)"
            }), 400

        plot_base_url = os.getenv('TIMESHEET_PLOT_BASE_URL', '')
        image_url = str(data.get('image_url', '')).strip()
        if not image_url:
            image_url = _build_report_url(plot_base_url, sheet_date)

        if not image_url:
            return jsonify({
                "error": "ไม่พบ image_url และไม่สามารถประกอบ URL จาก TIMESHEET_PLOT_BASE_URL"
            }), 400

        if not text:
            text = f"รายงาน Timesheet วันที่ {sheet_date}" if sheet_date else "รายงาน Timesheet ล่าสุด"

        host = os.getenv('MQTT_HOST', '').strip()
        port = int(os.getenv('MQTT_PORT', '1883'))
        username = os.getenv('MQTT_USERNAME', '').strip()
        password = os.getenv('MQTT_PASSWORD', '').strip()
        topic = os.getenv('MQTT_REPLY_TOPIC', 'ai_timesheet_reply').strip()
        to = os.getenv('MQTT_REPLY_TO', 'line_webhook').strip()

        if not host:
            return jsonify({"error": "ไม่พบ MQTT_HOST ใน .env"}), 400

        can_send, reason = _begin_send_once(sheet_date)
        if not can_send:
            return jsonify({
                "message": f"ข้ามการส่งซ้ำ ({reason})",
                "group_id": group_id,
                "sheet_date": sheet_date or None,
                "image_url": image_url,
            }), 200

        payload = _build_mqtt_line_payload(
            to=to,
            source_id=group_id,
            source_type=source_type,
            text=text,
            image_url=image_url,
            preview_image_url=image_url,
        )

        mqtt_publish.single(
            topic=topic,
            payload=json.dumps(payload, ensure_ascii=False),
            hostname=host,
            port=port,
            auth={"username": username, "password": password} if username else None,
        )
        _finish_send_once(sheet_date, True)

        print(f"✅ ส่งรายงานเข้า LINE กลุ่มแล้ว: {group_id} ({sheet_date or 'latest'})")
        return jsonify({
            "message": "ส่งคำสั่งส่งรูปเข้ากลุ่มแล้ว",
            "group_id": group_id,
            "sheet_date": sheet_date or None,
            "image_url": image_url,
            "topic": topic,
            "to": to,
        }), 200

    except Exception as e:
        _finish_send_once(sheet_date if 'sheet_date' in locals() else '', False)
        print(f"❌ Error send_report_to_group: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/line_timesheet_command', methods=['POST'])
def line_timesheet_command():
    """
    รับข้อความจาก LINE bot แล้วสั่งส่งรายงานเข้า LINE กลุ่ม/ห้อง/ผู้ใช้

    รองรับข้อความ:
    - Timesheet
    - Timesheet DDMMYY
    - @Bot Timesheet
    - @Bot Timesheet DD/MM/YY

    Request Body (JSON):
    {
        "text": "Timesheet 210426",
        "source_id": "Cxxxxxxxx",      # optional (default LINE_GROUP_ID)
        "source_type": "group"         # optional: group|room|user
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        text = str(
            data.get('text')
            or (data.get('message') or {}).get('text')
            or ''
        ).strip()

        parsed = _parse_timesheet_text_command(text)
        if not parsed.get('handled'):
            return jsonify({
                "handled": False,
                "message": parsed.get('error', 'ไม่ใช่คำสั่ง Timesheet')
            }), 200

        if parsed.get('error'):
            return jsonify({
                "handled": True,
                "error": parsed['error']
            }), 400

        sheet_date = parsed.get('sheet_date', '')
        if parsed.get('use_latest'):
            sheet_date = _get_latest_sheet_date() or ''

        if not sheet_date:
            return jsonify({
                "handled": True,
                "error": "ไม่พบข้อมูลวันที่ล่าสุดในระบบ"
            }), 404

        if not _sheet_date_exists(sheet_date):
            return jsonify({
                "handled": True,
                "error": "ไม่พบข้อมูลวันที่ระบุ",
                "sheet_date": sheet_date
            }), 404

        can_send, reason = _begin_send_once(sheet_date)
        if not can_send:
            return jsonify({
                "handled": True,
                "message": f"ข้ามการส่งซ้ำ ({reason})",
                "sheet_date": sheet_date
            }), 200

        source_id = str(data.get('source_id', '')).strip()
        source_type = str(data.get('source_type', 'group')).strip() or 'group'
        text_out = f"รายงาน Timesheet ประจำวันที่ {_format_sheet_date_display(sheet_date)}"

        send_result = _publish_report_to_group(
            sheet_date=sheet_date,
            text=text_out,
            group_id=source_id,
            source_type=source_type,
        )
        success = bool(send_result.get('ok'))
        _finish_send_once(sheet_date, success)

        if not success:
            return jsonify({
                "handled": True,
                "error": send_result.get('error', 'ส่งไม่สำเร็จ'),
                "sheet_date": sheet_date
            }), 400

        return jsonify({
            "handled": True,
            "message": "ส่งรายงานเข้ากลุ่มแล้ว",
            "sheet_date": sheet_date,
            "group_id": send_result.get('group_id'),
            "source_type": send_result.get('source_type'),
            "image_url": send_result.get('image_url'),
        }), 200

    except Exception as e:
        _finish_send_once(sheet_date if 'sheet_date' in locals() else '', False)
        print(f"❌ Error line_timesheet_command: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/send/<sheet_date>')
def send_report_short(sheet_date):
    """ส่งรายงานเข้า LINE กลุ่มแบบสั้น: GET /send/<sheet_date>"""
    try:
        sheet_date = (sheet_date or '').strip()
        if len(sheet_date) != 6 or not sheet_date.isdigit():
            return jsonify({"error": "รูปแบบวันที่ต้องเป็น DDMMYY เช่น 210426"}), 400

        if not _sheet_date_exists(sheet_date):
            return jsonify({"error": "ไม่พบข้อมูลวันที่ระบุ", "sheet_date": sheet_date}), 404

        can_send, reason = _begin_send_once(sheet_date)
        if not can_send:
            return jsonify({"message": f"ข้ามการส่งซ้ำ ({reason})", "sheet_date": sheet_date}), 200

        result = _publish_report_to_group(sheet_date=sheet_date)
        _finish_send_once(sheet_date, bool(result.get('ok')))
        if not result.get('ok'):
            return jsonify({"error": result.get('error', 'ส่งไม่สำเร็จ')}), 400

        return jsonify({
            "message": "ส่งรายงานเข้ากลุ่มแล้ว",
            "sheet_date": sheet_date,
            "group_id": result.get('group_id'),
            "image_url": result.get('image_url'),
        }), 200
    except Exception as e:
        _finish_send_once(sheet_date if 'sheet_date' in locals() else '', False)
        print(f"❌ Error send_report_short: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/send')
def send_report_short_latest():
    """ส่งรายงานล่าสุดเข้า LINE กลุ่มแบบสั้น: GET /send"""
    latest = _get_latest_sheet_date()
    if not latest:
        return jsonify({"error": "ไม่พบข้อมูลใน Database"}), 404
    return send_report_short(latest)

# ==========================================
# API: หน้าแรก
# ==========================================
@app.route('/')
def index():
    return jsonify({
        "message": "Time Sheet Report API (SQLite Version)",
        "endpoints": {
            "checkin": {
                "method": "POST",
                "url": "/checkin",
                "description": "รับข้อมูลเช็คอิน + Auto-resolve ชื่อสถานที่อัตโนมัติใน Background",
                "body": {
                    "objectid"    : 1324,
                    "peauser"     : "U001",
                    "pullfullname": "สมชาย ใจดี",
                    "pullposition": "วิศวกร",
                    "checktime"   : "08:30",
                    "stampdate"   : "10/03/26",
                    "y"           : 13.7563,
                    "x"           : 100.5018,
                    "oper"        : "",
                    "commenttext" : ""
                }
            },
            "update_locations": {
                "method": "POST",
                "url": "/update_locations",
                "description": "อัพเดตพิกัดเป็นชื่อสถานที่ผ่าน Geocoding อัตโนมัติ",
                "body": {
                    "all": True,
                    "or": "objectids: [1324, 1328]"
                }
            },
            "rename_location": {
                "method": "POST",
                "url": "/rename_location",
                "description": "ตั้งชื่อสถานที่ด้วยตัวเอง (Manual)",
                "body_single": {
                    "objectid": 1324,
                    "location_name": "สำนักงานใหญ่"
                },
                "body_batch": {
                    "updates": [
                        {"objectid": 1324, "location_name": "สำนักงานใหญ่"},
                        {"objectid": 1328, "location_name": "สาขาลาดพร้าว"}
                    ]
                }
            },
            "plot_report": {
                "method": "GET",
                "url": "/plotdailylog/<sheet_date>",
                "description": "สร้างรายงาน Time Sheet + ส่งเข้ากลุ่ม LINE",
                "example": "/plotdailylog/170226",
                "note": "ข้อความที่ส่ง: รายงาน Timesheet ประจำวันที่ <sheet_date>"
            },
            "plot_report_latest": {
                "method": "GET",
                "url": "/plotdailylog",
                "description": "สร้างรายงานวันล่าสุด + ส่งเข้ากลุ่ม LINE",
                "note": "ข้อความที่ส่ง: รายงาน Timesheet ประจำวันที่ ล่าสุด"
            },
            "send_report_to_group": {
                "method": "POST",
                "url": "/send_report_to_group",
                "description": "ส่งรูปภาพรายงานเข้า LINE กลุ่มผ่าน MQTT bridge",
                "body": {
                    "sheet_date": "170226",
                    "text": "รายงาน Timesheet ประจำวันที่ 17/02/26"
                }
            },
            "send_short": {
                "method": "GET",
                "url": "/send/<sheet_date>",
                "description": "คำสั่งสั้น ส่งรายงานเข้ากลุ่ม LINE โดยตรง (ส่งซ้ำติดๆ กันจะถูกข้าม)",
                "example": "/send/210426"
            },
            "line_timesheet_command": {
                "method": "POST",
                "url": "/line_timesheet_command",
                "description": "รับข้อความจากบอท: Timesheet หรือ Timesheet <sheet_date> แล้วส่งรายงานเข้ากลุ่ม LINE",
                "body": {
                    "text": "Timesheet 210426",
                    "source_id": "Cxxxxxxxx (optional)",
                    "source_type": "group (optional)"
                }
            }
        }
    })

# ==========================================
# Run Flask App
# ==========================================
if __name__ == '__main__':
    print("=" * 70)
    print("🚀 Starting Time Sheet Report API Server (SQLite Version)")
    print("=" * 70)
    print("📍 API 0: POST /checkin          - รับข้อมูลเช็คอิน + Auto-resolve ชื่อสถานที่")
    print("📍 API 1: POST /update_locations - อัพเดตพิกัดเป็นชื่อสถานที่ (Geocoding)")
    print("📍 API 2: POST /rename_location  - ตั้งชื่อสถานที่ด้วยตัวเอง (Manual)")
    print("📍 1: GET  /plotdailylog/<sheet_date> - สร้างรายงาน + ส่งเข้ากลุ่ม LINE")
    print("📍 2: GET  /plotdailylog             - สร้างรายงานวันล่าสุด + ส่งเข้ากลุ่ม LINE")
    print("📍 API 4: POST /send_report_to_group     - ส่งรูปเข้ากลุ่ม LINE ผ่าน MQTT")
    print("📍 API 5: GET  /send/<sheet_date>         - ส่งรูปเข้ากลุ่ม LINE (คำสั่งสั้น)")
    print("📍 API 6: GET  /send                      - ส่งรูปล่าสุดเข้ากลุ่ม LINE")
    print("📍 API 7: POST /line_timesheet_command    - รับข้อความ Timesheet/Timesheet <sheet_date> แล้วส่งเข้ากลุ่ม LINE")
    print("=" * 70)
    print("\n🎨 ตัวอย่างการใช้งาน:")
    print("  # อัพเดตชื่อสถานที่ด้วยตัวเอง (แบบเดี่ยว)")
    print('  curl -X POST http://localhost:5002/rename_location \\')
    print('       -H "Content-Type: application/json" \\')
    print('       -d \'{"objectid": 1324, "location_name": "สำนักงานใหญ่"}\'')
    print("\n  # อัพเดตชื่อสถานที่ด้วยตัวเอง (แบบหลายรายการ)")
    print('  curl -X POST http://localhost:5002/rename_location \\')
    print('       -H "Content-Type: application/json" \\')
    print('       -d \'{"updates": [{"objectid": 1324, "location_name": "สำนักงานใหญ่"}, {"objectid": 1328, "location_name": "สาขา"}]}\'')
    print("\n  # อัพเดตทั้งหมดด้วย Geocoding")
    print('  curl -X POST http://localhost:5002/update_locations \\')
    print('       -H "Content-Type: application/json" \\')
    print('       -d \'{"all": true}\'')
    print("\n  # อัพเดตเฉพาะ objectid")
    print('  curl -X POST http://localhost:5002/update_locations \\')
    print('       -H "Content-Type: application/json" \\')
    print('       -d \'{"objectids": [1324, 1328, 1349]}\'')
    print("\n  # สร้างรายงาน + ส่งเข้ากลุ่ม LINE (ระบุวัน)")
    print('  curl http://localhost:5002/plotdailylog/170226 -o report.png')
    print("\n  # สร้างรายงาน + ส่งเข้ากลุ่ม LINE (วันล่าสุด)")
    print('  curl http://localhost:5002/plotdailylog -o report.png')
    print("\n  # ส่งรูปเข้ากลุ่ม LINE")
    print('  curl -X POST http://localhost:5002/send_report_to_group \\')
    print('       -H "Content-Type: application/json" \\')
    print('       -d \'{"sheet_date":"170226","text":"รายงาน Timesheet ประจำวันที่ 17/02/26"}\'')
    print("\n  # ส่งรูปเข้ากลุ่ม LINE (คำสั่งสั้น)")
    print('  curl http://localhost:5002/send/210426')
    print("\n  # รับคำสั่งจากบอท (ข้อความ Timesheet)")
    print('  curl -X POST http://localhost:5002/line_timesheet_command \\')
    print('       -H "Content-Type: application/json" \\')
    print("       -d '{\"text\":\"Timesheet 210426\"}'")
    print("=" * 70)
    
    app.run(
        debug=os.getenv("TIMESHEET_DEBUG", "1").strip() in {"1", "true", "True"},
        host='0.0.0.0',
        port=int(os.getenv("TIMESHEET_PORT", "5002")),
    )
