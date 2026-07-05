import os
import sys
import json
import uuid
import subprocess
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_batch import WriteBatch

# 1. إعداد Firebase
def init_firebase():
    # يتم تمرير محتوى ملف الـ Service Account كـ Environment Variable لحماية البيانات
    cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not cred_json:
        print("خطأ: لم يتم العثور على مفتاح Firebase!")
        sys.exit(1)
        
    cred_dict = json.loads(cred_json)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    return firestore.client()

# 2. تحميل الفيديو باستخدام yt-dlp واستخراج البيانات
def download_video(video_url):
    video_id = str(uuid.uuid4()) # توليد معرف فريد محلي للمشروع
    output_raw = f"raw_{video_id}.mp4"
    
    print("🔷 جاري تحميل الفيديو واستخراج البيانات...")
    # أمر yt-dlp لتحميل الفيديو واستخراج الـ JSON الخاص به
    cmd = [
        "yt-dlp",
        "--dump-json",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "-o", output_raw,
        video_url
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
    if result.returncode != 0:
        print(f"خطأ أثناء التحميل: {result.stderr}")
        sys.exit(1)
        
    video_meta = json.loads(result.stdout)
    return video_id, output_raw, video_meta

# 3. تعديل الفيديو والصوت لتخطي الـ Content ID وتغيير الـ MD5
def bypass_content_id(input_file, video_id):
    output_processed = f"processed_{video_id}.mp4"
    print("⚡ جاري معالجة الفيديو لتخطي الـ Content ID وتغيير البصمة الرقمية...")
    
    # فلتر FFmpeg احترافي:
    # - تغيير الحجم بنسبة ضئيلة جداً (scale)
    # - تعديل طفيف جداً في الألوان والسطوع (eq)
    # - تسريع الفيديو بنسبة 1.01 (setpts) لتغيير التوقيت والـ FPS بدون ملاحظة
    # - تعديل درجة الصوت ونسبته ضئيل جداً (atedge / atempo)
    
    ffmpeg_cmd = [
        "ffmpeg", "-i", input_file,
        "-vf", "scale=iw*1.002:ih*1.002,eq=brightness=0.005:contrast=1.01,setpts=0.99*PTS",
        "-af", "atempo=1.01,volume=1.02",
        "-r", "30", # توحيد الـ Frame Rate لتغيير البصمة
        "-c:v", "libx264", "-crf", "20", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        output_processed
    ]
    
    res = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"خطأ أثناء معالجة الفيديو بـ FFmpeg: {res.stderr}")
        sys.exit(1)
        
    return output_processed

# 4. الرفع إلى GitHub Releases والحصول على الرابط الثابت
def upload_to_github_release(file_path, video_id):
    repo = os.environ.get("GITHUB_REPOSITORY") # يأتي تلقائياً من GitHub Actions
    token = os.environ.get("GITHUB_TOKEN")
    tag = "videos"
    
    print("🚀 جاري رفع الفيديو إلى GitHub Releases...")
    
    # التأكد من وجود الـ Release أو إنشائه
    release_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    
    res = requests.get(release_url, headers=headers)
    
    if res.status_code == 404:
        # إنشاء Release جديد إذا لم يكن موجوداً
        create_url = f"https://api.github.com/repos/{repo}/releases"
        data = {"tag_name": tag, "title": "AI Managed Videos Storage", "draft": False, "prerelease": False}
        res = requests.post(create_url, headers=headers, json=data)
        release_id = res.json()["id"]
    else:
        release_id = res.json()["id"]
        
    # رفع ملف الفيديو كـ Asset
    upload_url = f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets?name={video_id}.mp4"
    headers["Content-Type"] = "video/mp4"
    
    with open(file_path, "rb") as f:
        upload_res = requests.post(upload_url, headers=headers, data=f)
        
    if upload_res.status_code not in [201, 200]:
        print(f"خطأ أثناء الرفع لـ GitHub: {upload_res.text}")
        sys.exit(1)
        
    download_url = upload_res.json()["browser_download_url"]
    return download_url

# 5. حفظ البيانات في Firestore دفعة واحدة (ACID Transaction/Batch)
def save_to_firestore_acid(db, video_id, meta, download_url):
    print("💾 جاري تسجيل البيانات في Firestore وتطبيق مبدأ ACID...")
    
    # تجهيز البيانات التفصيلية (ملف الفيديو المستقل)
    video_details = {
        "id": video_id,
        "title": meta.get("title", "Unknown Title"),
        "description": meta.get("description", ""),
        "duration": meta.get("duration", 0),
        "tags": meta.get("tags", []),
        "download_url": download_url,
        "raw_metadata": json.dumps(meta)[:50000], # حفظ البيانات الخام بحد أقصى تجنباً للحجم
        "status": "new",
        "created_at": firestore.SERVER_TIMESTAMP
    }
    
    # تجهيز البيانات الخفيفة للفهرس الرئيسي (index)
    video_index = {
        "id": video_id,
        "title": meta.get("title", "Unknown Title"),
        "duration": meta.get("duration", 0),
        "type": "shorts" if meta.get("duration", 0) <= 60 else "long",
        "status": "new",
        "download_url": download_url,
        "created_at": firestore.SERVER_TIMESTAMP
    }
    
    # استخدام Firestore Write Batch لضمان الـ ACID (تنجح العمليتان معاً أو تفشلا معاً)
    batch = db.batch()
    
    # مرجع المجلد التفصيلي (data/videos)
    video_ref = db.collection("data").document("videos").collection("all_videos").document(video_id)
    batch.set(video_ref, video_details)
    
    # مرجع الفهرس الرئيسي (master_index)
    index_ref = db.collection("data").document("master_index").collection("items").document(video_id)
    batch.set(index_ref, video_index)
    
    # تنفيذ الـ Batch دفعة واحدة
    batch.commit()
    print("✅ تم حفظ البيانات بنجاح تام وتحديث الفهرس!")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("الرجاء إدخال رابط الفيديو!")
        sys.exit(1)
        
    url = sys.argv[1]
    
    # تنفيذ الدورة بالترتيب
    db = init_firebase()
    v_id, raw_file, metadata = download_video(url)
    processed_file = bypass_content_id(raw_file, v_id)
    final_url = upload_to_github_release(processed_file, v_id)
    save_to_firestore_acid(db, v_id, metadata, final_url)
    
    # تنظيف الملفات المؤقتة لتوفير مساحة الـ Runner
    if os.path.exists(raw_file): os.remove(raw_file)
    if os.path.exists(processed_file): os.remove(processed_file)
    print("🎉 اكتملت العملية بنجاح!")
