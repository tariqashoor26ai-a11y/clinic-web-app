import sqlite3
import requests
from flask import Flask, request, jsonify, render_template, g
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
DATABASE = 'clinic.db'

# ========================================================
# إعدادات سيرفر الواتساب (WAHA)
# ========================================================
WAHA_API_URL = "https://wahamy-whatsapp-ap.onrender.com" # استبدله برابط سيرفر الواتساب الخاص بك
WAHA_SESSION = "clinic1" # استبدله باسم الجلسة التي أنشأتها عند مسح الـ QR

# ========================================================
# ترقية قاعدة البيانات
# ========================================================
def upgrade_db():
    with sqlite3.connect(DATABASE) as db:
        cur = db.cursor()
        try:
            cur.execute("ALTER TABLE appointments ADD COLUMN reminder_sent INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS patients_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL
                )
            ''')
            try:
                cur.execute("INSERT OR IGNORE INTO patients_new (id, name, phone) SELECT id, name, phone FROM patients")
                cur.execute("DROP TABLE patients")
            except sqlite3.OperationalError:
                pass 
                
            cur.execute("ALTER TABLE patients_new RENAME TO patients")
            db.commit()
            print("[Database] تم تهيئة قاعدة البيانات بنجاح.")
        except Exception as e:
            print(f"[Database Error] {e}")

upgrade_db()

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# ========================================================
# دالة إرسال رسائل الواتساب
# ========================================================
def send_whatsapp_message(phone_number, message_text):
    """إرسال رسالة عبر محرك WAHA"""
    # محرك واتساب يطلب صيغة محددة للرقم تنتهي بـ @c.us
    chat_id = f"{phone_number}@c.us"
    url = f"{WAHA_API_URL}/api/sendText"
    
    payload = {
        "session": WAHA_SESSION,
        "chatId": chat_id,
        "text": message_text
    }
    
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[WhatsApp Error] فشل الإرسال: {e}")

# ========================================================
# بوابة استقبال ردود المرضى (Webhook)
# ========================================================
@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    """هذه الدالة تستقبل الردود آلياً من سيرفر الواتساب"""
    data = request.json
    
    # محرك WAHA يرسل أنواعاً كثيرة من البيانات، نحن نهتم فقط بـ "الرسائل النصية القادمة"
    if not data or data.get("event") != "message":
        return jsonify({"status": "ignored"}), 200

    payload = data.get("payload", {})
    from_number_full = payload.get("from", "")
    text = str(payload.get("body", "")).strip()
    
    # استخراج رقم الهاتف الصافي (إزالة @c.us من النهاية)
    phone = from_number_full.split('@')[0]
    
    if text in ['1', '2']:
        new_status = 'Confirmed' if text == '1' else 'Cancelled'
        reply_text = "تم تأكيد موعدك بنجاح! شكراً لك." if text == '1' else "تم إلغاء الموعد."
        
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        cur = db.cursor()
        
        # جلب كل المرضى المرتبطين بهذا الرقم (لدعم العائلات)
        cur.execute("SELECT id FROM patients WHERE phone = ?", (phone,))
        patients = cur.fetchall()
        
        if patients:
            for p in patients:
                cur.execute("UPDATE appointments SET status = ? WHERE patient_id = ? AND status = 'Scheduled'", (new_status, p['id']))
            db.commit()
            
            # الرد على المريض لتأكيد العملية
            send_whatsapp_message(phone, reply_text)
            print(f"[Success] تم تحديث مواعيد الرقم {phone} إلى {new_status}")
        db.close()
        
    return jsonify({"status": "success"}), 200

# ========================================================
# المجدول الزمني للإرسال الآلي (Cron Job)
# ========================================================
def auto_send_reminders():
    try:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        cur = db.cursor()
        
        cur.execute('''
            SELECT a.id, p.name, p.phone, a.appointment_date 
            FROM appointments a 
            JOIN patients p ON a.patient_id = p.id 
            WHERE a.status = 'Scheduled' AND (a.reminder_sent = 0 OR a.reminder_sent IS NULL)
        ''')
        appointments = cur.fetchall()
        
        for appt in appointments:
            msg = f"مرحباً {appt['name']}،\nنذكرك بموعدك في العيادة بتاريخ {appt['appointment_date']}.\n\nلتأكيد الحضور أرسل (1)\nلإلغاء الموعد أرسل (2)"
            send_whatsapp_message(appt['phone'], msg)
            
            cur.execute("UPDATE appointments SET reminder_sent = 1 WHERE id = ?", (appt['id'],))
            print(f"[Sender] تم إرسال تذكير عبر الواتساب إلى {appt['name']} ({appt['phone']})")
            
        db.commit()
        db.close()
    except Exception as e:
        print(f"[Sender Error] {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=auto_send_reminders, trigger="interval", minutes=1)
scheduler.start()

# ========================================================
# مسارات العيادة والواجهة
# ========================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/appointments', methods=['GET'])
def get_appointments():
    cur = get_db().cursor()
    cur.execute('''
        SELECT a.id, p.name, p.phone, a.appointment_date, a.status 
        FROM appointments a
        JOIN patients p ON a.patient_id = p.id
        ORDER BY a.appointment_date ASC
    ''')
    return jsonify([dict(row) for row in cur.fetchall()])

@app.route('/appointments', methods=['POST'])
def add_appointment():
    data = request.json
    db = get_db()
    cur = db.cursor()
    
    cur.execute("SELECT id FROM patients WHERE phone = ? AND name = ?", (data['phone'], data['name']))
    patient = cur.fetchone()
    
    if patient:
        patient_id = patient['id']
    else:
        cur.execute("INSERT INTO patients (name, phone) VALUES (?, ?)", (data['name'], data['phone']))
        patient_id = cur.lastrowid
        
    cur.execute("INSERT INTO appointments (patient_id, appointment_date, status, reminder_sent) VALUES (?, ?, 'Scheduled', 0)", (patient_id, data['appointment_date']))
    db.commit()
    return jsonify({"message": "تم إضافة الموعد بنجاح"}), 201

@app.route('/appointments/<int:appt_id>', methods=['DELETE'])
def delete_appointment(appt_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM appointments WHERE id = ?", (appt_id,))
    db.commit()
    return jsonify({"message": "تم المسح بنجاح"}), 200

@app.route('/trigger-reminders', methods=['POST'])
def manual_trigger():
    auto_send_reminders()
    return jsonify({"message": "تمت عملية الفحص والإرسال بنجاح"}), 200

if __name__ == '__main__':
    app.run(debug=False, port=5000)
