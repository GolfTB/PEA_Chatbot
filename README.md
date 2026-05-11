# PEA Chatbot (MQTT + LINE + Timesheet)

เอกสารนี้สรุปวิธีรันโปรเจกต์, การ build ด้วย Docker, เปิด ngrok และการรันไฟล์หลักแต่ละตัว

## โครงสร้างบริการหลัก
- `line_webhook.py` : LINE webhook (พอร์ตเริ่มต้น 5001)
- `timesheet.py` : Timesheet API (พอร์ตเริ่มต้น 5002)
- `app_ev.py` : MQTT worker / Attendance AI

## สิ่งที่ต้องมี
- Python 3.11+
- Docker
- ngrok

## ตั้งค่าไฟล์ .env
สร้างไฟล์ `.env` ในโฟลเดอร์นี้ แล้วใส่ตัวแปรหลัก เช่น

```
LINE_CHANNEL_ACCESS_TOKEN=...
LINE_CHANNEL_SECRET=...
LINE_BOT_USER_ID=...
MQTT_HOST=...
MQTT_USERNAME=...
MQTT_PASSWORD=...
MQTT_TOPIC=ai_timesheet
MQTT_TO=ai_timesheet
MQTT_FROM=line_webhook
MQTT_REPLY_TOPIC=ai_timesheet_reply
MQTT_REPLY_TO=line_webhook
TIMESHEET_API_URL=http://localhost:5002
LINE_WEBHOOK_PORT=5001
TIMESHEET_PORT=5002
```

> ถ้าใช้ค่าอื่น ให้แก้ใน `.env` ให้ตรงกับระบบที่รันจริง

## ติดตั้งและรันแบบ Local
```bash
cd /Users/pakornkiatpherpradab/Documents/code/PEA/agentic_AI/mqtt/PEA_Chatbot
python3 -m venv PEA_venv
source PEA_venv/bin/activate
pip install -r requirements.txt
```

### 1) รัน Timesheet API
```bash
python3 timesheet.py
```
- เปิดที่ `http://localhost:5002`

### 2) รัน LINE Webhook
```bash
python3 line_webhook.py
```
- เปิดที่ `http://localhost:5001/webhook`

### 3) รัน Attendance AI (MQTT Worker) ด้วย Docker Compose

**Docker Compose มีแค่ `app_ev` service ที่ mount `basic_agent_langchain_tool.py` เอง**

```bash
# จากโฟลเดอร์นี้ รัน app_ev container
docker compose up --build -d

# ตรวจสอบ container
docker compose ps

# ดู log
docker compose logs -f pea_app_ev

# หยุด
docker compose down
```

## เปิด ngrok สำหรับ LINE Webhook
```bash
ngrok http 5001
```
- เอา public URL ที่ได้ไปใส่ใน LINE Developer Console (Webhook URL)

## เช็คพอร์ตที่ใช้
- LINE Webhook: 5001 (กำหนดได้ด้วย `LINE_WEBHOOK_PORT`)
- Timesheet API: 5002 (กำหนดได้ด้วย `TIMESHEET_PORT`)

## การรันบนเครื่องอื่น (Multi-Machine Deployment)

ถ้าต้องการย้ายโปรเจกต์ไปรันบนเครื่องอื่น (Server, VPS, เครื่องอื่นในเครือข่าย) ต้องเตรียมดังนี้

### ขั้นตอนหลัก

#### 1. คัดลอก/Clone โปรเจกต์
```bash
git clone <repository-url> /path/to/project
cd /path/to/project/mqtt/PEA_Chatbot
```

#### 2. ตั้งค่า .env ใหม่ให้ตรงกับเครื่องที่รัน
ที่สำคัญต้องเปลี่ยน:

```env
# MQTT Broker - เปลี่ยน HOST ให้ตรงกับเครื่องที่รัน MQTT ของคุณ
MQTT_HOST=192.168.x.x    # หรือ mqtt.example.com (ไม่ใช่ localhost)
MQTT_USERNAME=your_username
MQTT_PASSWORD=your_password

# Timesheet API - ถ้า Timesheet รูน host คนละเครื่อง
TIMESHEET_API_URL=http://192.168.x.x:5002    # หรือ http://timesheet.example.com:5002

# LINE Webhook - ถ้ารันบน VPS ให้ใช้ IP/domain จริง (สำหรับ ngrok)
LINE_WEBHOOK_PORT=5001    # หรือเปลี่ยนจากค่า default ถ้า port ชน

# ngrok - ตั้งค่า auth token (ถ้ามี)
# NGROK_AUTHTOKEN=...
```

#### 3. ติดตั้ง Dependencies
```bash
python3 -m venv venv
source venv/bin/activate    # บน Linux/Mac
# source venv\Scripts\activate    # บน Windows
pip install -r requirements.txt
```

#### 4. รัน Services

**ในเครื่องเดียว (All-in-one)**
```bash
# Terminal 1: Timesheet
python3 timesheet.py

# Terminal 2: LINE Webhook
python3 line_webhook.py

# Terminal 3: App EV (Docker)
docker compose up --build -d

# Terminal 4: ngrok (optional)
ngrok http 5001
```

**หรือแบบแยก Host**
- Host A (MQTT Broker): เช่อ MQTT broker เท่านั้น
- Host B: `python3 timesheet.py` + `python3 line_webhook.py` + `ngrok http 5001`
- Host C: `docker compose up --build -d` (app_ev เท่านั้น)

ปรับ `.env` ให้ `MQTT_HOST` ชี้ไปที่ Host A จริง

### ตัวอย่างสถานการณ์

#### A) ทั้งหมดรัน host เดียว (VPS)
```bash
docker compose up --build -d
python3 timesheet.py &
python3 line_webhook.py &
ngrok http 5001
```
.env:
```
MQTT_HOST=192.168.1.100
TIMESHEET_API_URL=http://localhost:5002
```

#### B) Timesheet + LINE Webhook บน Host หนึ่ง, App EV บน Host อื่น
```
Server A: Timesheet + LINE Webhook (Local Python)
python3 timesheet.py &
python3 line_webhook.py &
ngrok http 5001

Server B: App EV + MQTT (Docker)
docker compose up --build -d

.env (Server B):
MQTT_HOST=192.168.1.10    # MQTT Broker address
TIMESHEET_API_URL=http://192.168.1.20:5002    # Timesheet Server IP
```

### เช็ครายละเอียดสำคัญ
1. **Port ไม่ชน**: เช็คว่า port 5001, 5002 ว่างทั้งหมด หรือต้องเปลี่ยน
2. **Firewall**: ถ้า host ต่างเครือข่าย เปิด port ใน firewall
3. **Database Paths**: `attendance.db`, `employees.db` ต้องอยู่ในโฟลเดอร์ PEA_Chatbot หรือแก้ path ใน code
4. **Network**: MQTT Broker ต้องเข้าถึงได้จากทุก services

### ปัญหาทั่วไป
- **MQTT ต่อไม่ได้**: ตรวจสอบ `MQTT_HOST` ว่าเป็น IP/hostname ของเครื่องที่รัน Broker จริง
- **Timesheet เรียกไม่ได้**: ตรวจสอบ `TIMESHEET_API_URL` และ firewall เปิด port 5002
- **LINE ตอบไม่ได้**: ตรวจสอบ ngrok running และ webhook URL ใน LINE Developer Console ถูกต้อง

## Troubleshooting สั้นๆ
- ถ้า MQTT ต่อไม่ได้ ให้เช็ค `MQTT_HOST`, `MQTT_USERNAME`, `MQTT_PASSWORD` (ต้อง IP/hostname จริง ไม่ใช่ localhost)
- ถ้า LINE ตอบไม่ได้ ให้เช็ค `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET` และ ngrok ว่า running
- ถ้า Timesheet เรียกไม่ได้ ให้เช็ค `TIMESHEET_API_URL` ว่า IP/hostname ถูก
