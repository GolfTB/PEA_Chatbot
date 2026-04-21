import os
import json
import sqlite3
from typing import Any
from datetime import datetime

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    from langchain_openai import ChatOpenAI
except Exception:
    ChatOpenAI = None

try:
    from langchain_core.tools import tool
except Exception:
    tool = None

try:
    from langchain.agents import AgentExecutor, create_tool_calling_agent
except Exception:
    AgentExecutor = None
    create_tool_calling_agent = None

try:
    from langchain.agents import create_agent
except Exception:
    create_agent = None

try:
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
except Exception:
    ChatPromptTemplate = None
    MessagesPlaceholder = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "whooutside.db")
TABLE_NAME = "whooutside"


def load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(os.path.join(BASE_DIR, "api_key.env"))
        load_dotenv(os.path.join(BASE_DIR, ".env"))


def ensure_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                Employee_ID TEXT NOT NULL,
                Leave_Date TEXT NOT NULL,
                Type TEXT NOT NULL,
                Reason TEXT
            )
            """
        )
        conn.commit()


def _map_category_to_type(category: str) -> str:
    if category == "ไปราชการ":
        return "ลาไปราชการ"
    if category in {"ลา", "WFH", "ระบุไม่ได้"}:
        return category
    return "ระบุไม่ได้"


def _parse_payload_json(payload_json: str, user_text: str = "") -> dict[str, Any]:
    cleaned = payload_json.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("payload_json ต้องเป็น JSON object")

    category = str(data.get("category", "ระบุไม่ได้")).strip() or "ระบุไม่ได้"
    dates = data.get("dates", [])
    reason = str(data.get("reason", "-")).strip() or "-"

    if not isinstance(dates, list) or not all(isinstance(d, str) for d in dates):
        raise ValueError("dates ต้องเป็น list ของ string")
    if not dates:
        raise ValueError("dates ต้องมีอย่างน้อย 1 วัน")

    return {"category": category, "dates": dates, "reason": reason}


def _is_unspecified_category(category: str) -> bool:
    normalized = (category or "").strip()
    return normalized == "ระบุไม่ได้"


def _insert_whooutside_records(payload_json: str, employee_id: str = "UNKNOWN", user_text: str = "") -> int:
    data = _parse_payload_json(payload_json, user_text=user_text)
    if _is_unspecified_category(data["category"]):
        raise ValueError("category เป็นระบุไม่ได้ จึงไม่บันทึกข้อมูลลงฐานข้อมูล")

    leave_type = _map_category_to_type(data["category"])

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        for leave_date in data["dates"]:
            cur.execute(
                f"INSERT INTO {TABLE_NAME} (Employee_ID, Leave_Date, Type, Reason) VALUES (?, ?, ?, ?)",
                (employee_id, leave_date, leave_type, data["reason"]),
            )
        conn.commit()

    return len(data["dates"])


if tool is not None:
    @tool
    def tool_insert_whooutside(payload_json: str, employee_id: str = "UNKNOWN") -> str:
        """บันทึกข้อมูลลา/นอกสถานที่ลงฐานข้อมูลจาก payload JSON"""
        inserted = _insert_whooutside_records(payload_json=payload_json, employee_id=employee_id)
        return f"inserted {inserted} row(s)"
else:
    def tool_insert_whooutside(payload_json: str, employee_id: str = "UNKNOWN") -> str:
        inserted = _insert_whooutside_records(payload_json=payload_json, employee_id=employee_id)
        return f"inserted {inserted} row(s)"


SYSTEM_PROMPT = """
คุณเป็นผู้ช่วยอัจฉริยะฝ่ายบุคคล (HR Assistant) ทำหน้าที่สกัดข้อมูลจากการแจ้งลางานหรือไปปฏิบัติงานนอกสถานที่

กฎการทำงานแบบ Script (ต้องทำตามลำดับ):
1) กำหนดค่าเริ่มต้น
    - วันนี้คือวันที่: {current_date}
    - ค่าที่อนุญาตสำหรับ category มีแค่: "ลา", "WFH", "ไปราชการ", "ระบุไม่ได้"

2) Intent Gate (กรองก่อนว่าเป็นคำขอ HR หรือไม่)
    - ถ้าเป็นแค่คำทักทาย/คุยทั่วไป/ไม่ใช่คำขอ เช่น "สวัสดี", "หวัดดี", "ขอบคุณ", "เทส", "วันนี้อากาศดี", "ม้าและลา"
      => category="ระบุไม่ได้", reason="ระบุไม่ได้"
    - ถ้ามีเจตนาแจ้งลา/WFH/ไปราชการ ให้ไปขั้นถัดไป

3) Category Mapping (จับหมวดแบบตายตัว)
    - ถ้ามีความหมายไปทาง WFH (เช่น "WFH", "ทำงานที่บ้าน") => category="WFH"
    - ถ้ามีความหมายไปทางไปราชการ (เช่น "ไปราชการ", "อบรม", "ดูงาน", "พบลูกค้า", "นอกสถานที่") => category="ไปราชการ"
    - ถ้ามีความหมายไปทางลา (เช่น "ลา", "ลากิจ", "ลาป่วย", "ลาพักร้อน") => category="ลา"
    - ถ้าพูดถึงอาการป่วย/รักษา (เช่น "ป่วย", "ไม่สบาย", "ไข้", "ไปหาหมอ", "ไปโรงพยาบาล") แม้ไม่มีคำว่า "ลา" => category="ลา"
    - ถ้าไม่เข้าเงื่อนไขใดเลย => category="ระบุไม่ได้"

4) Date Extraction
    - ต้องได้ dates เป็น list ของ "YYYY-MM-DD"
    - ลำดับความสำคัญ: วันที่ชัดเจน > "วันที่ <เลขวัน> นี้" > ช่วงวันที่ > วันสัมพัทธ์ (วันนี้/พรุ่งนี้/...)
    - ถ้าระบุจำนวนวัน ให้เริ่มจากวันนี้ และนับเฉพาะวันทำงาน (จันทร์-ศุกร์)
    - ห้ามมีเสาร์-อาทิตย์ในผลลัพธ์ ถ้าตรงเสาร์-อาทิตย์ให้เลื่อนไปวันทำการถัดไป
    - ถ้าไม่พบวันที่ ให้ใช้ ["{current_date}"]

5) Reason Extraction
    - reason ต้องเป็นเหตุผลหลักสั้น ๆ
    - ถ้าเจอข้อความหลัง "เพราะ/เนื่องจาก/ด้วย" ให้ใช้ส่วนนั้นเป็นหลัก
    - ถ้าเป็นลาป่วย ให้ใช้อาการหรือสาเหตุเจ็บป่วยเป็น reason
    - ถ้าหาเหตุผลเฉพาะไม่ได้ แต่เป็นคำขอ HR ชัดเจน ให้ reason="ระบุไม่ได้"
    - ถ้าไม่ใช่คำขอ HR ให้ reason="ระบุไม่ได้"

6) Output Contract
    - ตอบเป็น JSON เท่านั้น
    - key ต้องเรียง: category, dates, reason
    - ห้ามใช้ค่า category นอก 4 ค่า

7) ตัวอย่างบังคับ
    - "ขอลา 1 วันครับ" => category="ลา"
    - "ลาป่วยวันนี้" => category="ลา", reason ควรเป็น "ป่วย"
    - "วันนี้ WFH" => category="WFH"
    - "สวัสดี" => category="ระบุไม่ได้"
    - "ม้าและลา" => category="ระบุไม่ได้"

รูปแบบผลลัพธ์:
{{
  "category": "ลา หรือ WFH หรือ ไปราชการ หรือ ระบุไม่ได้",
  "dates": ["YYYY-MM-DD"],
  "reason": "..."
}}
""".strip()

def get_llm() -> Any:
    if ChatOpenAI is None:
        raise RuntimeError("ยังไม่พบแพ็กเกจ langchain-openai")

    api_key = (os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("ไม่พบ API key. ให้เพิ่ม OPENROUTER_API_KEY หรือ OPENAI_API_KEY")

    return ChatOpenAI(
        model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        temperature=0,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
    )


def ask_llm(user_text: str) -> str:
    llm = get_llm()
    current_date = datetime.now().strftime("%Y-%m-%d")
    system_prompt = SYSTEM_PROMPT.format(current_date=current_date)

    result = llm.invoke([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ])

    return getattr(result, "content", "") or "{}"


def process_line_message(res: str, rep: str, employee_id: str = "UNKNOWN") -> dict[str, Any]:
    """ประมวลผลข้อความจาก LINE แล้วบันทึกลง whooutside

    Args:
        res: ข้อความจากผู้ใช้ LINE
        rep: LINE replyToken
        employee_id: รหัสพนักงาน
    """
    load_env()
    ensure_db()

    user_text = (res or "").strip()
    reply_token = (rep or "").strip()
    employee = (employee_id or "UNKNOWN").strip() or "UNKNOWN"

    if not user_text:
        return {
            "rep": reply_token,
            "employee_id": employee,
            "payload_json": "{}",
            "inserted": 0,
            "response_text": "กรุณาระบุข้อความที่ต้องการแจ้งลา/WFH/ไปราชการ",
        }

    payload_json = ask_llm(user_text)
    parsed = _parse_payload_json(payload_json, user_text=user_text)

    if _is_unspecified_category(parsed["category"]):
        response_text = (
            "ยังไม่บันทึกคำขอ เนื่องจากระบบระบุประเภทไม่ชัดเจน\n"
            "กรุณาระบุประเภท (ลา/WFH/ไปราชการ) และวันลาให้ชัดเจน แล้วส่งใหม่อีกครั้ง\n"
            "ตัวอย่าง: ขอลาวันที่ 2026-04-22 เพราะไปโรงพยาบาล"
        )
        return {
            "rep": reply_token,
            "employee_id": employee,
            "payload_json": payload_json,
            "inserted": 0,
            "response_text": response_text,
        }

    inserted = _insert_whooutside_records(payload_json=payload_json, employee_id=employee, user_text=user_text)

    dates = ", ".join(parsed["dates"])
    response_text = (
        f"บันทึกคำขอเรียบร้อยแล้ว\n"
        f"พนักงาน: {employee}\n"
        f"ประเภท: {parsed['category']}\n"
        f"วันที่: {dates}\n"
        f"เหตุผล: {parsed['reason']}\n"
        # f"จำนวนรายการที่บันทึก: {inserted}"
    )

    return {
        "rep": reply_token,
        "employee_id": employee,
        "payload_json": payload_json,
        "inserted": inserted,
        "response_text": response_text,
    }


def create_agent_executor() -> tuple[Any, str]:
    if tool is None:
        raise RuntimeError("ยังไม่พบแพ็กเกจ langchain-core tools")
    llm = get_llm()
    current_date = datetime.now().strftime("%Y-%m-%d")
    base_prompt = SYSTEM_PROMPT.format(current_date=current_date)
    agent_system_prompt = (
        base_prompt
        + "\n\nกติกาการใช้ tool สำหรับ Agent:\n"
        + "- ใช้ได้เฉพาะ tool_insert_whooutside เท่านั้น\n"
        + "- สำหรับข้อความแจ้งลา/WFH/ไปราชการ ต้องเรียก tool_insert_whooutside 1 ครั้งเสมอ\n"
        + "- ค่า payload_json ที่ส่งเข้า tool_insert_whooutside ต้องเป็น JSON ที่มี key: category, dates, reason\n"
        + "- หลังเรียก tool แล้ว ให้ตอบกลับผู้ใช้เป็น JSON เดิมเท่านั้น\n"
    )

    tools = [tool_insert_whooutside]

    if create_agent is not None:
        agent = create_agent(
            model=llm,
            tools=tools,
            system_prompt=agent_system_prompt,
        )
        return agent, "graph"

    if AgentExecutor is None or create_tool_calling_agent is None:
        raise RuntimeError("ยังไม่พบแพ็กเกจ langchain ที่รองรับ agent")
    if ChatPromptTemplate is None or MessagesPlaceholder is None:
        raise RuntimeError("ยังไม่พบแพ็กเกจ langchain-core prompts")

    prompt = ChatPromptTemplate.from_messages([
        ("system", agent_system_prompt),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])

    agent = create_tool_calling_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=False), "executor"


def _extract_agent_output(result: Any) -> str:
    if isinstance(result, dict):
        output = result.get("output")
        if isinstance(output, str) and output.strip():
            return output

        messages = result.get("messages")
        if isinstance(messages, list) and messages:
            last_msg = messages[-1]
            content = getattr(last_msg, "content", None)

            if isinstance(content, str) and content.strip():
                return content

            if isinstance(last_msg, dict):
                dict_content = last_msg.get("content", "")
                if isinstance(dict_content, str) and dict_content.strip():
                    return dict_content

        return json.dumps(result, ensure_ascii=False)

    return str(result)


def main() -> None:
    load_env()
    ensure_db()
    sample_res = (os.getenv("LINE_RES") or "").strip()
    sample_rep = (os.getenv("LINE_REP") or "TEST_REPLY_TOKEN").strip()
    sample_employee_id = (os.getenv("LINE_EMPLOYEE_ID") or "UNKNOWN").strip() or "UNKNOWN"

    if not sample_res:
        print("พร้อมใช้งานผ่าน process_line_message(res, rep, employee_id)")
        print("หากต้องการทดสอบแบบ one-shot ให้ตั้ง LINE_RES, LINE_REP, LINE_EMPLOYEE_ID แล้วรันไฟล์นี้")
        return

    # line_event = {...}
    # user_text = line_event["message"]["text"]
    # -------------------------------------------------------------------------
    # ตัวอย่างแนวทาง (คอมเมนต์ไว้) สำหรับ "เชื่อมฐานข้อมูล + สร้าง tool + ดึงมาใช้ซ้ำ"
    #
    # แนวคิด:
    # 1) แยกส่วน DB เป็นฟังก์ชันใช้งานซ้ำ (connect/init/insert/list)
    # 2) ห่อฟังก์ชัน DB เป็น LangChain tools (@tool)
    # 3) ตอนอยากใช้ agent ค่อย bind tools เข้า model หรือ agent executor
    #
    # ------------------------------------------------------------
    # [A] reusable DB layer (โค้ดแกนกลาง ใช้ซ้ำได้หลายที่)
    #
    # import sqlite3
    #
    # def get_db_conn(db_path: str = "whooutside.db"):
    #     # จุดเดียวสำหรับเปิด connection -> ปรับง่ายเวลาเปลี่ยน DB
    #     return sqlite3.connect(db_path)
    #
    # def ensure_whooutside_db(db_path: str = "whooutside.db") -> None:
    #     # สร้างตารางถ้ายังไม่มี
    #     conn = get_db_conn(db_path)
    #     cur = conn.cursor()
    #     cur.execute("""
    #         CREATE TABLE IF NOT EXISTS whooutside (
    #             id INTEGER PRIMARY KEY AUTOINCREMENT,
    #             Employee_ID TEXT NOT NULL,
    #             Leave_Date TEXT NOT NULL,
    #             Type TEXT NOT NULL,
    #             Reason TEXT
    #         )
    #     """)
    #     conn.commit()
    #     conn.close()
    #
    # def insert_whooutside_records(payload_json: str, employee_id: str = "UNKNOWN", db_path: str = "whooutside.db") -> None:
    #     # รับ JSON ที่ได้จาก ask_llm แล้วบันทึกลง DB
    #     import json
    #     data = json.loads(payload_json)
    #     category = str(data.get("category", "LEAVE")).upper()
    #     dates = data.get("dates", [])
    #     reason = data.get("reason", "-") or "-"
    #
    #     leave_type = "ลาไปราชการ" if category == "OUTSIDE" else "ลากิจ"
    #
    #     conn = get_db_conn(db_path)
    #     cur = conn.cursor()
    #     for d in dates:
    #         cur.execute(
    #             "INSERT INTO whooutside (Employee_ID, Leave_Date, Type, Reason) VALUES (?, ?, ?, ?)",
    #             (employee_id, d, leave_type, reason),
    #         )
    #     conn.commit()
    #     conn.close()
    #
    # ------------------------------------------------------------
    # [B] tool layer (ห่อฟังก์ชัน DB ให้ agent เรียกได้)
    #
    # from langchain_core.tools import tool
    #
    # @tool
    # def tool_insert_leave(payload_json: str, employee_id: str = "UNKNOWN") -> str:
    #     """บันทึกข้อมูลลา/นอกสถานที่ลงฐานข้อมูล"""
    #     insert_whooutside_records(payload_json=payload_json, employee_id=employee_id)
    #     return "inserted"
    #
    # @tool
    # def tool_list_latest(limit: int = 10) -> str:
    #     """ดูข้อมูลล่าสุดจากตาราง whooutside"""
    #     conn = get_db_conn()
    #     cur = conn.cursor()
    #     cur.execute(
    #         "SELECT Employee_ID, Leave_Date, Type, Reason FROM whooutside ORDER BY id DESC LIMIT ?",
    #         (limit,),
    #     )
    #     rows = cur.fetchall()
    #     conn.close()
    #     return str(rows)
    #
    # ------------------------------------------------------------
    # [C] reuse ในโหมด agent (ถ้าจะเปิดใช้ในอนาคต)
    #
    # # tools = [tool_insert_leave, tool_list_latest]
    # # llm = get_llm()
    # # llm_with_tools = llm.bind_tools(tools)
    # # result = llm_with_tools.invoke([
    # #     {"role": "system", "content": "..."},
    # #     {"role": "user", "content": user_text},
    # # ])
    #
    # # หรืออีกแบบ: สร้าง agent_executor แล้วส่ง tools ชุดเดิมเข้าไป
    # # จุดสำคัญคือ "เขียน DB function ทีเดียว" แล้วห่อเป็น tool เพื่อ reuse ได้หลาย flow
    # -------------------------------------------------------------------------
 
    try:
        result = process_line_message(
            res=sample_res,
            rep=sample_rep,
            employee_id=sample_employee_id,
        )
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(f"เกิดข้อผิดพลาด: {exc}")


if __name__ == "__main__":
    main()
