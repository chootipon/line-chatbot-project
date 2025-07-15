import os
from flask import Flask, request, abort, render_template, redirect, url_for, flash
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai
import json # สำหรับโหลด Service Account Key
import time # สำหรับจับเวลา
import logging # สำหรับ Logging ที่ดีขึ้นใน Flask

# ตั้งค่า Logging ให้เห็น DEBUG message ใน Render logs
logging.basicConfig(level=logging.INFO) # เริ่มต้นเป็น INFO
app_logger = logging.getLogger('app_logger') # สร้าง logger ของตัวเอง
app_logger.setLevel(logging.DEBUG) # ตั้งระดับเป็น DEBUG สำหรับ logger นี้

# --- Firebase Imports ---
import firebase_admin
from firebase_admin import credentials, firestore

# --- กำหนดค่า Config (ต้องเปลี่ยนเป็นค่าของคุณเอง) ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GOOGLE_GEMINI_API_KEY = os.getenv("GOOGLE_GEMINI_API_KEY")
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "your_super_secret_key_for_flask_messages") # ควรเปลี่ยนเป็นคีย์ที่ซับซ้อนกว่านี้

# ตรวจสอบว่าได้ตั้งค่า Environment Variables แล้ว
if not LINE_CHANNEL_ACCESS_TOKEN: raise ValueError("LINE_CHANNEL_ACCESS_TOKEN is not set.")
if not LINE_CHANNEL_SECRET: raise ValueError("LINE_CHANNEL_SECRET is not set.")
if not GOOGLE_GEMINI_API_KEY: raise ValueError("GOOGLE_GEMINI_API_KEY is not set.")
if not FIREBASE_SERVICE_ACCOUNT_JSON: raise ValueError("FIREBASE_SERVICE_ACCOUNT_JSON is not set.")

# --- ตั้งค่า Firebase ---
try:
    # โหลด Service Account Key จาก string JSON ที่อยู่ใน Environment Variable
    cred_json = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
    cred = credentials.Certificate(cred_json)
    firebase_admin.initialize_app(cred)
    db = firestore.client() # ได้ Instance ของ Firestore
    app_logger.info("Firebase initialized successfully!")
except Exception as e:
    app_logger.error(f"Error initializing Firebase: {e}")
    # ใน Production อาจจะใช้ Sentry/Cloud Logging เพื่อจับ error นี้และทำให้ App หยุดทำงาน
    exit(1)

# กำหนดค่า Gemini Model
genai.configure(api_key=GOOGLE_GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-pro') # สามารถใช้ 'gemini-1.5-pro' หากคุณเข้าถึงได้และต้องการความสามารถที่สูงขึ้น

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY # ตั้งค่า Secret Key สำหรับ Flask Flash Messages

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- ข้อมูล Schema สำหรับ Gemini (อธิบายโครงสร้าง Firestore) ---
# สิ่งนี้ช่วยให้ Gemini เข้าใจ "ประเภท" ของข้อมูลที่เรามีใน Firestore
FIRESTORE_SCHEMA_DESCRIPTION = """
เรามีข้อมูลสินค้าใน Firebase Firestore ใน Collection ชื่อ 'products'
แต่ละเอกสาร (document) ใน Collection 'products' มี fields ดังนี้:
- id (string): รหัสสินค้า (นี่คือ document ID ของ Firestore)
- name (string): ชื่อสินค้า (เช่น iPhone 15, MacBook Air M3, Keyboard)
- price (float): ราคาสินค้า (เช่น 35000.00, 45000.00)
- stock (integer): จำนวนสินค้าในคลัง (เช่น 100, 50, 200)
- category (string): หมวดหมู่สินค้า (เช่น Smartphones, Laptops, Accessories)

ข้อมูลนี้ใช้สำหรับตอบคำถามทั่วไปเกี่ยวกับสินค้า, ราคา, สต็อก, หมวดหมู่, หรือข้อมูลเฉพาะของสินค้า.
"""

# --- ฟังก์ชันสำหรับดึงข้อมูลจาก Firestore ตามเจตนา ---
def get_product_data(action, query_params=None):
    products_ref = db.collection('products')
    result_docs = []

    app_logger.debug(f"get_product_data called with action='{action}' and query_params='{query_params}'")
    try:
        if action == "fetch_all_products":
            docs = products_ref.stream()
            for doc in docs:
                result_docs.append(doc.to_dict())
            app_logger.debug(f"Fetched {len(result_docs)} documents for fetch_all_products.")
            return result_docs
        
        elif action == "fetch_by_name" and query_params and 'name' in query_params:
            product_name = query_params['name']
            app_logger.debug(f"Fetching by name: {product_name}")
            docs = products_ref.where('name', '==', product_name).stream()
            for doc in docs:
                result_docs.append(doc.to_dict())
            app_logger.debug(f"Fetched {len(result_docs)} documents for name '{product_name}'.")
            return result_docs
        
        elif action == "fetch_by_category" and query_params and 'category' in query_params:
            category_name = query_params['category']
            app_logger.debug(f"Fetching by category: {category_name}")
            docs = products_ref.where('category', '==', category_name).stream()
            for doc in docs:
                result_docs.append(doc.to_dict())
            app_logger.debug(f"Fetched {len(result_docs)} documents for category '{category_name}'.")
            return result_docs
        
        # คุณสามารถเพิ่ม action อื่นๆ ได้ที่นี่ เช่น:
        # - "fetch_low_stock": ดึงสินค้าที่สต็อกเหลือน้อย
        # - "fetch_expensive_items": ดึงสินค้าที่มีราคาสูง
        
        app_logger.debug(f"Action '{action}' not recognized or missing query_params.")
        return None # ถ้าไม่ตรงกับ action ที่รู้จัก
    
    except Exception as e:
        app_logger.error(f"Firestore data retrieval error: {e}")
        return f"เกิดข้อผิดพลาดในการดึงข้อมูลจาก Firebase: {e}"

# --- Webhook Endpoint สำหรับ LINE OA ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app_logger.info("Request body: " + body) # Debug: ควรจะเห็น Request body ใน Logs

    try:
        # !!! จุดที่สำคัญที่สุด: handler.handle ควรจะเรียก handler.add ที่คุณเห็นด้านล่าง !!!
        handler.handle(body, signature)
    except InvalidSignatureError:
        app_logger.error("Invalid signature. Check your channel secret.") # Debug: ถ้ามีปัญหา signature จะเห็น Error นี้
        abort(400) # Bad Request
    except Exception as e: # !!! เพิ่มจุดนี้เพื่อจับ Exception ที่ handler.handle อาจจะไม่ได้จัดการ !!!
        app_logger.exception(f"FATAL ERROR: Unhandled exception in handler.handle during LINE webhook processing: {e}")
        # ใช้ app_logger.exception เพื่อให้ได้ full traceback ใน logs
        abort(500) # Internal Server Error
    return 'OK'

# --- Event Handler สำหรับ Text Message ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    reply_message = "ขออภัยค่ะ ไม่เข้าใจคำถามของคุณ ลองถามใหม่นะคะ"

    app_logger.debug(f"\n--- New Message from LINE ---") # Debug: บรรทัดแรกที่คุณควรเห็น
    app_logger.debug(f"User message received: '{user_message}'") # Debug: ควรเห็นข้อความผู้ใช้
    start_total_time = time.time()

    try:
        # --- ขั้นตอนที่ 1: ให้ Gemini ระบุ "เจตนา" และข้อมูลที่ต้องการจากคำถามผู้ใช้ ---
        start_gemini_intent_time = time.time()
        intent_prompt = f"""
        คุณคือผู้ช่วย AI ที่เชี่ยวชาญในการทำความเข้าใจคำถามและระบุข้อมูลที่จำเป็นจากฐานข้อมูลสินค้า.
        ฐานข้อมูลของเรามี Collection 'products' ที่มีโครงสร้างดังนี้:
        {FIRESTORE_SCHEMA_DESCRIPTION}

        จากคำถามของผู้ใช้ โปรดระบุ "action" ที่เหมาะสมที่สุดเพื่อดึงข้อมูลจากฐานข้อมูล.
        และระบุ "query_params" ที่จำเป็นสำหรับ action นั้นๆ.
        
        รูปแบบการตอบกลับต้องเป็น JSON เท่านั้น. ห้ามมีข้อความอื่นใดๆ เพิ่มเติม.

        Possible actions:
        - "fetch_all_products": เมื่อผู้ใช้ต้องการข้อมูลสินค้าทั้งหมด (ไม่มี query_params)
        - "fetch_by_name": เมื่อผู้ใช้ถามถึงข้อมูลเฉพาะของสินค้าด้วยชื่อ (query_params: {{"name": "ชื่อสินค้า"}})
        - "fetch_by_category": เมื่อผู้ใช้ถามถึงสินค้าในหมวดหมู่ใดหมวดหมู่หนึ่ง (query_params: {{"category": "ชื่อหมวดหมู่"}})
        - "unknown": เมื่อไม่สามารถระบุ action ได้ (ไม่มี query_params)

        ตัวอย่างการตอบกลับ:
        - สำหรับ "มีสินค้าอะไรบ้าง": {{"action": "fetch_all_products"}}
        - สำหรับ "ราคา iPhone 15 เท่าไหร่": {{"action": "fetch_by_name", "query_params": {{"name": "iPhone 15"}}}}
        - สำหรับ "สินค้าหมวด Laptops มีอะไรบ้าง": {{"action": "fetch_by_category", "query_params": {{"category": "Laptops"}}}}
        - สำหรับ "สวัสดี": {{"action": "unknown"}}

        คำถามจากผู้ใช้: "{user_message}"

        JSON Response:
        """
        
        intent_response = model.generate_content(intent_prompt)
        intent_json_str = intent_response.text.strip()
        app_logger.debug(f"Time for Intent generation: {time.time() - start_gemini_intent_time:.2f} seconds")
        app_logger.debug(f"Gemini Intent JSON: {intent_json_str}")

        action = "unknown"
        query_params = None
        try:
            intent_data = json.loads(intent_json_str)
            action = intent_data.get('action')
            query_params = intent_data.get('query_params')
            app_logger.debug(f"Parsed action: '{action}', params: '{query_params}'")
        except json.JSONDecodeError:
            app_logger.error(f"Failed to parse JSON from Gemini: {intent_json_str}")
            # action ยังคงเป็น "unknown" ตามค่าเริ่มต้น

        retrieved_data = None
        if action != "unknown":
            retrieved_data = get_product_data(action, query_params)
            if isinstance(retrieved_data, str): # ถ้ามี error จาก Firestore (ฟังก์ชัน get_product_data คืนค่าเป็น string)
                reply_message = retrieved_data
                app_logger.error(f"Firestore data retrieval failed: {reply_message}")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message))
                return # จบการทำงานตรงนี้ถ้ามี error ในการดึงข้อมูล
            app_logger.debug(f"Retrieved data from Firestore: {json.dumps(retrieved_data, ensure_ascii=False)}")
        else:
            app_logger.debug(f"Action is 'unknown', skipping data retrieval.")

        # --- ขั้นตอนที่ 2: ให้ Gemini สังเคราะห์คำตอบจากข้อมูลที่ดึงมา ---
        start_gemini_answer_time = time.time()
        answer_prompt = f"""
        ผู้ใช้ถามคำถาม: "{user_message}"

        นี่คือข้อมูลที่เรามีจากฐานข้อมูลสินค้า (Firebase Firestore):
        {json.dumps(retrieved_data, indent=2, ensure_ascii=False) if retrieved_data else "ไม่พบข้อมูลที่เกี่ยวข้อง"}

        โปรดตอบคำถามของผู้ใช้ด้วยภาษาที่เป็นธรรมชาติและเป็นประโยชน์ โดยอ้างอิงจากข้อมูลที่ให้มา.
        ถ้าข้อมูลที่ให้มาไม่เพียงพอที่จะตอบคำถามได้ ให้ตอบกลับอย่างสุภาพว่าไม่พบข้อมูลที่เกี่ยวข้อง.
        หลีกเลี่ยงการตอบว่า "ไม่สามารถดำเนินการ" หรือ "ไม่พบข้อมูลที่ตรงกับคำถามของคุณค่ะ" หากคุณสามารถสรุปจากข้อมูลที่มีได้.
        
        ตัวอย่างการตอบ:
        - ถ้าถามราคา iPhone 15 และมีข้อมูล: "iPhone 15 มีราคา 35,000 บาทค่ะ"
        - ถ้าถามสินค้าหมวด Laptops และมีข้อมูล: "สินค้าในหมวด Laptops ได้แก่ MacBook Air M3 (ราคา 45,000 บาท) และ Dell XPS 15 (ราคา 55,000 บาท) ค่ะ"
        - ถ้าถามเรื่องสต็อก: "สินค้า iPhone 15 มีสต็อก 100 ชิ้นค่ะ"
        - ถ้าถามสิ่งที่ข้อมูลไม่มี: "ขออภัยค่ะ ไม่พบข้อมูลเกี่ยวกับเรื่องนั้นในฐานข้อมูลของเราในขณะนี้"

        คำตอบ:
        """
        
        final_answer_response = model.generate_content(answer_prompt)
        reply_message = final_answer_response.text.strip()
        app_logger.debug(f"Time for Answer generation: {time.time() - start_gemini_answer_time:.2f} seconds")
        app_logger.debug(f"Final reply message to LINE: '{reply_message}'")

        if not reply_message: # ถ้าข้อความเป็นค่าว่าง
            reply_message = "ขออภัยค่ะ ไม่สามารถสร้างคำตอบได้ในขณะนี้ โปรดลองอีกครั้ง."
            app_logger.warning("Reply message was empty, setting fallback.")

    except Exception as e:
        app_logger.exception(f"FATAL ERROR: Unhandled exception in handle_message: {e}")
        reply_message = "เกิดข้อผิดพลาดในการประมวลผล กรุณาลองใหม่อีกครั้งค่ะ"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_message)
    )
    app_logger.debug(f"--- End of Message Handling (Total Time: {time.time() - start_total_time:.2f} seconds) ---")


# --- Route สำหรับหน้า Admin Dashboard (ใช้ Firestore) ---
@app.route("/admin")
def admin_dashboard():
    products_ref = db.collection('products')
    products = []
    try:
        for doc in products_ref.stream():
            product_data = doc.to_dict()
            product_data['id'] = doc.id 
            products.append(product_data)
        app_logger.debug(f"Loaded {len(products)} products for admin dashboard.")
    except Exception as e:
        app_logger.error(f"Failed to load products for admin dashboard: {e}")
        flash(f"เกิดข้อผิดพลาดในการโหลดสินค้า: {e}", "danger")
    return render_template("admin.html", products=products)

# --- Route สำหรับเพิ่มสินค้า (ใช้ Firestore) ---
@app.route("/admin/add_product", methods=['GET', 'POST'])
def add_product():
    if request.method == 'POST':
        product_data = {
            'name': request.form['name'],
            'price': float(request.form['price']),
            'stock': int(request.form['stock']),
            'category': request.form['category']
        }
        try:
            doc_ref = db.collection('products').document()
            doc_ref.set(product_data)
            flash(f"สินค้าถูกเพิ่มเรียบร้อยแล้ว! (ID: {doc_ref.id})", "success")
            app_logger.debug(f"Added product: {product_data['name']} with ID: {doc_ref.id}")
        except Exception as e:
            flash(f"เกิดข้อผิดพลาดในการเพิ่มสินค้า: {e}", "danger")
            app_logger.error(f"Failed to add product: {e}")
        return redirect(url_for('admin_dashboard'))
    return render_template("add_product.html")

# --- Route สำหรับแก้ไขสินค้า (ใช้ Firestore) ---
@app.route("/admin/edit_product/<string:product_id>", methods=['GET', 'POST'])
def edit_product(product_id):
    doc_ref = db.collection('products').document(product_id)
    product_doc = doc_ref.get()

    if not product_doc.exists:
        flash("ไม่พบสินค้าที่ต้องการแก้ไข", "danger")
        app_logger.debug(f"Product ID '{product_id}' not found for editing.")
        return redirect(url_for('admin_dashboard'))

    product = product_doc.to_dict()
    product['id'] = product_doc.id 

    if request.method == 'POST':
        updated_data = {
            'name': request.form['name'],
            'price': float(request.form['price']),
            'stock': int(request.form['stock']),
            'category': request.form['category']
        }
        try:
            doc_ref.set(updated_data, merge=True) 
            flash("สินค้าถูกแก้ไขเรียบร้อยแล้ว!", "success")
            app_logger.debug(f"Updated product ID: {product_id} with data: {updated_data}")
        except Exception as e:
            flash(f"เกิดข้อผิดพลาดในการแก้ไขสินค้า: {e}", "danger")
            app_logger.error(f"Failed to update product ID: {product_id} - {e}")
        return redirect(url_for('admin_dashboard'))
    
    return render_template("edit_product.html", product=product)

# --- Route สำหรับลบสินค้า (ใช้ Firestore) ---
@app.route("/admin/delete_product/<string:product_id>", methods=['POST'])
def delete_product(product_id):
    try:
        db.collection('products').document(product_id).delete()
        flash("สินค้าถูกลบเรียบร้อยแล้ว!", "success")
        app_logger.debug(f"Deleted product ID: {product_id}")
    except Exception as e:
        flash(f"เกิดข้อผิดพลาดในการลบสินค้า: {e}", "danger")
        app_logger.error(f"Failed to delete product ID: {product_id} - {e}")
    return redirect(url_for('admin_dashboard'))

# --- รัน Flask App ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000)) # Render จะกำหนด PORT ให้
    app.run(host='0.0.0.0', port=port) # รันบน 0.0.0.0 เพื่อให้เข้าถึงได้จากภายนอก
