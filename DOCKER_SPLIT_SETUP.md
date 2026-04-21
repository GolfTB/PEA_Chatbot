# Docker Split Setup (Machine 1 = Webhook, Machine 2 = AI Worker)

โครงนี้ออกแบบให้รัน 2 เครื่องแยกกัน:
- Machine 1: `line_webhook.py` + `ngrok`
- Machine 2: `app_ev.py` + `basic_agent_langchain_tool.py`

## 1) เตรียมไฟล์บนแต่ละเครื่อง

ใช้โค้ดโฟลเดอร์เดียวกัน: `agentic_AI/mqtt/PEA_Chatbot`

### Machine 1 (Webhook)
1. สร้างไฟล์ env
   - คัดลอกจาก `.env.webhook.example` เป็น `.env.webhook`
2. เตรียมฐานข้อมูลพนักงาน
   - วางไฟล์ `employees.db` ไว้ในโฟลเดอร์เดียวกับ compose

### Machine 2 (AI Worker)
1. สร้างไฟล์ env
   - คัดลอกจาก `.env.worker.example` เป็น `.env.worker`
2. เตรียมฐานข้อมูล attendance
   - วางไฟล์ `whooutside.db` ไว้ในโฟลเดอร์เดียวกับ compose

## 2) Build + Run แยกเครื่อง

### Machine 1
```bash
docker compose -f docker-compose.webhook.yml up -d --build
```

### Machine 2
```bash
docker compose -f docker-compose.worker.yml up -d --build
```

## 3) เปิด ngrok ที่ Machine 1

```bash
ngrok http 5001
```

นำ URL ที่ได้ไปตั้งใน LINE Developer Webhook เป็น:
- `https://<ngrok-id>.ngrok-free.app/callback`

## 4) ตรวจ log

### Machine 1
```bash
docker compose -f docker-compose.webhook.yml logs -f
```

### Machine 2
```bash
docker compose -f docker-compose.worker.yml logs -f
```

## 5) จุดสำคัญเรื่อง env

ค่าต่อไปนี้ต้องตรงกันทั้งสองเครื่อง:
- `MQTT_HOST`, `MQTT_USERNAME`, `MQTT_PASSWORD`
- Request route: Machine1 `MQTT_TOPIC` -> Machine2 `MQTT_OPERATOR`/`MQTT_INBOUND_TOPIC`
- Reply route: Machine2 `MQTT_REPLY_TOPIC`/`MQTT_REPLY_TO` -> Machine1 `MQTT_REPLY_TOPIC`/`MQTT_REPLY_TO`

ค่าชุดมาตรฐานที่ให้มาในตัวอย่างคือ:
- request topic/to: `ai_timesheet`
- reply topic/to: `ai_timesheet_reply` / `line_webhook`

## 6) หมายเหตุเรื่อง DB ภายนอก

ปัจจุบัน compose mount file จาก host เข้า container ดังนี้:
- Webhook: `./employees.db:/app/employees.db`
- Worker: `./whooutside.db:/app/whooutside.db`

ถ้าต้องการใช้ path ภายนอกอื่น ให้แก้ฝั่งซ้ายของ volume เช่น:
- `/data/pea/employees.db:/app/employees.db`
- `/data/pea/whooutside.db:/app/whooutside.db`

## 7) Stop

```bash
docker compose -f docker-compose.webhook.yml down
docker compose -f docker-compose.worker.yml down
```
