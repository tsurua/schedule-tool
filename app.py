"""
スケジュール調整ツール
カレンダーのスクショから空き日程を抽出して、グループの日程調整を簡単にする
"""
import streamlit as st
import json
import os
import re
import base64
import secrets as secret_module
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict, Counter

# ===== 設定 =====
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
EVENTS_FILE = DATA_DIR / "events.json"
SUBMISSIONS_FILE = DATA_DIR / "submissions.json"

MAX_PARTICIPANTS = 20
MAX_IMAGE_SIZE_MB = 5
WEEKDAY_JP = ['月', '火', '水', '木', '金', '土', '日']

def generate_time_slots(start_time, end_time, duration_h, duration_m):
    """時間帯を所要時間単位で区切ったスロット (start, end) のリストを返す"""
    duration_min = duration_h * 60 + duration_m
    if duration_min <= 0:
        return []
    s_h, s_m = map(int, start_time.split(':'))
    e_h, e_m = map(int, end_time.split(':'))
    s_total = s_h * 60 + s_m
    e_total = e_h * 60 + e_m
    slots = []
    cur = s_total
    while cur + duration_min <= e_total:
        st_str = f"{cur//60:02d}:{cur%60:02d}"
        en_str = f"{(cur+duration_min)//60:02d}:{(cur+duration_min)%60:02d}"
        slots.append((st_str, en_str))
        cur += duration_min
    return slots

# ===== データ永続化 =====
def load_json(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def load_events():
    return load_json(EVENTS_FILE)

def save_events(events):
    save_json(EVENTS_FILE, events)

def load_submissions():
    return load_json(SUBMISSIONS_FILE)

def save_submissions(subs):
    save_json(SUBMISSIONS_FILE, subs)

# ===== ユーティリティ =====
def gen_id(length=8):
    return secret_module.token_urlsafe(16)[:length]

def get_anthropic_key():
    try:
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        return os.environ.get("ANTHROPIC_API_KEY")

def get_gemini_key():
    try:
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        return os.environ.get("GEMINI_API_KEY")

def get_base_url():
    try:
        return st.secrets["BASE_URL"]
    except Exception:
        return os.environ.get("BASE_URL", "http://localhost:8501")

def fmt_date(d_str):
    d = datetime.strptime(d_str, '%Y-%m-%d').date()
    return f"{d.month}月{d.day}日（{WEEKDAY_JP[d.weekday()]}）"

# ===== AI 抽出 =====
def build_extraction_prompt(event):
    return f"""あなたはカレンダー画像から空き日程を抽出するアシスタントです。

【条件】
- 候補期間: {event['date_from']} 〜 {event['date_to']}
- 時間帯: {event['start_time']} 〜 {event['end_time']}
- 必要な所要時間: {event['duration_hours']}時間{event['duration_minutes']}分以上の連続した空き

【ルール】
1. 候補期間内の各日付について、上記の時間帯の中に必要な所要時間以上の連続した空きがあれば「空き」と判定
2. 予定の中身（タイトル・内容・人名）は絶対に出力しない(プライバシー保護)
3. 候補期間外の日付は無視
4. 空きがある日付だけを出力
5. visible_range には「画像で実際に視認できた日付範囲」を入れる（日付が読み取れない場合は null）

【出力形式】JSONのみ、説明文は一切なし:
{{"available_dates": ["YYYY-MM-DD", "YYYY-MM-DD", ...], "visible_range": {{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}}}}"""

def parse_extraction_response(text):
    """available_dates と visible_range を返す"""
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return [], None
    try:
        data = json.loads(m.group(0))
        dates = data.get("available_dates", [])
        vr = data.get("visible_range")
        return dates, vr
    except json.JSONDecodeError:
        return [], None

def parse_json_response(text):
    """旧APIとの互換のため、available_dates だけを返すバージョン"""
    dates, _ = parse_extraction_response(text)
    return dates

def extract_with_gemini(image_bytes, mime_type, event):
    """戻り値: (available_dates, visible_range)"""
    from google import genai
    from google.genai import types
    import time

    client = genai.Client(api_key=get_gemini_key())
    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    config = types.GenerateContentConfig(response_mime_type="application/json")
    contents = [image_part, build_extraction_prompt(event)]

    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite"]
    last_err = None
    for model in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model, contents=contents, config=config
                )
                return parse_extraction_response(response.text)
            except Exception as e:
                msg = str(e)
                last_err = e
                if "503" in msg or "429" in msg or "UNAVAILABLE" in msg or "RESOURCE_EXHAUSTED" in msg:
                    time.sleep(2 ** attempt)
                    continue
                break
    raise last_err

def extract_with_claude(image_bytes, mime_type, event):
    """戻り値: (available_dates, visible_range)"""
    from anthropic import Anthropic
    client = Anthropic(api_key=get_anthropic_key())
    img_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": img_b64}},
                {"type": "text", "text": build_extraction_prompt(event)},
            ]
        }]
    )
    return parse_extraction_response(response.content[0].text)

def extract_available_dates(image_bytes, mime_type, event):
    """戻り値: (available_dates, is_mock, visible_range)"""
    if get_gemini_key():
        try:
            dates, vr = extract_with_gemini(image_bytes, mime_type, event)
            return dates, False, vr
        except ImportError:
            st.warning("google-genai ライブラリが未インストールです。pip install google-genai を実行してください")
            return mock_extract(event), True, None
        except Exception as e:
            msg = str(e)
            if "503" in msg or "UNAVAILABLE" in msg:
                st.error("Gemini が一時的に混雑しています。1〜2分待ってからもう一度「AIで読み取る」を押してください。\n\n※ 解消しない場合は Anthropic API キーを設定すると Claude に自動切替されます")
            elif "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                st.error("Gemini の無料枠の利用上限に達しました（1分15回・1日1500回）。少し時間をおいてからもう一度お試しください")
            else:
                st.error(f"Gemini API エラー: {msg}")
            return [], False, None

    if get_anthropic_key():
        try:
            dates, vr = extract_with_claude(image_bytes, mime_type, event)
            return dates, False, vr
        except ImportError:
            return mock_extract(event), True, None
        except Exception as e:
            st.error(f"Claude API エラー: {e}")
            return [], False, None

    return mock_extract(event), True, None

def mock_extract(event):
    """API キー未設定時の模擬データ：候補期間中の土日を返す"""
    d_from = datetime.strptime(event['date_from'], '%Y-%m-%d').date()
    d_to = datetime.strptime(event['date_to'], '%Y-%m-%d').date()
    out = []
    cur = d_from
    while cur <= d_to and len(out) < 12:
        if cur.weekday() in [5, 6]:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out

# ===== スタイル =====
def inject_css():
    st.markdown("""
    <style>
    .stApp { background: #f9fafb; }
    .block-container { max-width: 720px; padding-top: 2rem; }
    .event-info { background:#f3f4f6; border-radius:12px; padding:16px; font-size:14px; }
    .url-card-pub { background:#eef2ff; border:2px solid #c7d2fe; border-radius:14px; padding:16px; margin-bottom:12px; }
    .url-card-adm { background:#fffbeb; border:2px solid #fde68a; border-radius:14px; padding:16px; margin-bottom:12px; }
    .privacy-note { background:#dbeafe; border-radius:8px; padding:12px; font-size:13px; color:#1e3a8a; margin:12px 0; }
    .done-card { text-align:center; padding:24px; }
    h1, h2, h3 { color: #111827; }
    </style>
    """, unsafe_allow_html=True)

# ===== 画面：イベント作成 =====
def show_create_event():
    st.markdown("# 📸 スクショで簡単！ 予定調整")
    st.caption("カレンダーのスクショから空き日程を抽出して、グループの日程調整を簡単にします")

    # 既に作成完了したイベントを表示中
    if "created_event" in st.session_state:
        show_created_urls(st.session_state.created_event)
        if st.button("← 新しいイベントを作る"):
            del st.session_state.created_event
            st.rerun()
        return

    with st.form("create_event"):
        st.markdown("### イベントを作る")

        name = st.text_input("イベント名", placeholder="例：ポジティブ心理学読書会・最終発表会")

        st.markdown("**📅 候補期間**（クリックでカレンダーが開きます）")
        col1, col2 = st.columns(2)
        with col1:
            date_from = st.date_input("📅 開始日", value=date.today() + timedelta(days=7), format="YYYY/MM/DD")
        with col2:
            date_to = st.date_input("📅 終了日", value=date.today() + timedelta(days=21), format="YYYY/MM/DD")

        time_options = [f"{h:02d}:{m:02d}" for h in range(24) for m in [0, 30]]
        st.markdown("**時間帯**")
        col3, col4 = st.columns(2)
        with col3:
            start_time = st.selectbox("開始時刻", time_options, index=time_options.index("19:00"), label_visibility="collapsed")
        with col4:
            end_time = st.selectbox("終了時刻", time_options, index=time_options.index("21:00"), label_visibility="collapsed")
        st.caption(f"参加者の都合をこの時間帯の中で見ます")

        st.markdown("**所要時間**")
        col5, col_colon, col6 = st.columns([5, 1, 5])
        with col5:
            duration_h = st.selectbox("時間", list(range(0, 9)), index=1, label_visibility="collapsed")
        with col_colon:
            st.markdown("<div style='text-align:center; font-size:24px; font-weight:600; padding-top:4px;'>:</div>", unsafe_allow_html=True)
        with col6:
            duration_m = st.selectbox("分", [0, 15, 30, 45], index=0, label_visibility="collapsed", format_func=lambda x: f"{x:02d}")
        st.caption("時間帯の中で、この長さの空きがある日を抽出します")

        mode = st.radio("開催形式", ["🏠 オンライン", "🤝 対面"], horizontal=True)

        submitted = st.form_submit_button("URLを発行する", type="primary", use_container_width=True)

        if submitted:
            if not name:
                st.error("イベント名を入力してください")
                return
            if date_to <= date_from:
                st.error("終了日は開始日より後にしてください")
                return
            if duration_h == 0 and duration_m == 0:
                st.error("所要時間を1分以上に設定してください")
                return
            # 時間帯と所要時間の整合性チェック
            sh, sm = map(int, start_time.split(':'))
            eh, em = map(int, end_time.split(':'))
            range_min = (eh * 60 + em) - (sh * 60 + sm)
            dur_min = duration_h * 60 + duration_m
            if range_min <= 0:
                st.error("時間帯の終了時刻は開始時刻より後にしてください")
                return
            if dur_min > range_min:
                st.error(f"所要時間（{duration_h}時間{duration_m:02d}分）が時間帯の長さ（{range_min//60}時間{range_min%60:02d}分）より長くなっています。設定を見直してください")
                return

            event_id = gen_id(8)
            admin_token = gen_id(12)
            events = load_events()
            events[event_id] = {
                "name": name,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "start_time": start_time,
                "end_time": end_time,
                "duration_hours": duration_h,
                "duration_minutes": duration_m,
                "mode": mode,
                "admin_token": admin_token,
                "created_at": datetime.now().isoformat(),
            }
            save_events(events)
            st.session_state.created_event = event_id
            st.rerun()

def show_created_urls(event_id):
    events = load_events()
    event = events.get(event_id)
    if not event:
        st.error("イベントが見つかりません")
        return

    base = get_base_url()
    p_url = f"{base}?event={event_id}"
    a_url = f"{base}?event={event_id}&admin={event['admin_token']}"

    st.markdown("# ✨ URLを発行しました")
    st.caption("下の2つのURLをそれぞれ使い分けてください")

    st.markdown("### 👥 参加者URL（みんなに配る）")
    st.code(p_url, language=None)

    st.markdown("### 🔐 管理URL（自分だけ・ブックマーク推奨）")
    st.code(a_url, language=None)
    st.warning("このURLを知っている人だけが集計を見られます。他の人に見せないでください")

    st.markdown("### 📝 イベント情報")
    d_from = datetime.strptime(event['date_from'], '%Y-%m-%d').date()
    d_to = datetime.strptime(event['date_to'], '%Y-%m-%d').date()
    st.markdown(f"""
- **イベント名**：{event['name']}
- **候補期間**：{d_from.strftime('%Y/%m/%d')} 〜 {d_to.strftime('%Y/%m/%d')}
- **時間帯**：{event['start_time']}〜{event['end_time']}
- **所要時間**：{event['duration_hours']}時間{event['duration_minutes']:02d}分
- **開催形式**：{event['mode']}
""")

# ===== 画面：参加者 =====
def show_participant(event_id):
    events = load_events()
    if event_id not in events:
        st.error("このイベントは存在しないか、期限切れです")
        return
    event = events[event_id]

    st.markdown(f"# 📚 {event['name']}")
    d_from = datetime.strptime(event['date_from'], '%Y-%m-%d').date()
    d_to = datetime.strptime(event['date_to'], '%Y-%m-%d').date()
    st.markdown(f"""
- 📅 候補期間：{d_from.strftime('%Y年%m月%d日')} 〜 {d_to.strftime('%Y年%m月%d日')}
- 🕐 時間帯：{event['start_time']}〜{event['end_time']}
- ⏱ 所要時間：{event['duration_hours']}時間{event['duration_minutes']:02d}分
- {event['mode']}
""")
    st.divider()

    if "p_step" not in st.session_state:
        st.session_state.p_step = 1

    if st.session_state.p_step == 1:
        participant_step1(event_id, event)
    elif st.session_state.p_step == 2:
        participant_step2(event_id, event)
    elif st.session_state.p_step == 3:
        participant_step3()

def participant_step1(event_id, event):
    d_from = datetime.strptime(event['date_from'], '%Y-%m-%d').date()
    d_to = datetime.strptime(event['date_to'], '%Y-%m-%d').date()

    # === プライバシー注意書き（最上部）===
    with st.container(border=True):
        st.markdown("#### 🔒 プライバシーについて（必ずお読みください）")
        st.markdown("""
- アップロードした画像は **AIが日付を読み取った直後に破棄** され、サーバーには保存されません
- **主催者にも画像は届きません**。送られるのは「日付・時間枠の文字情報」だけです
- 念のため、**予定の中身が見られたくない場合** は、カレンダーアプリの「予定の詳細を非表示」モードに切り替えてからスクショするのがおすすめです
""")

    st.markdown("### 1. お名前")
    name = st.text_input("お名前", placeholder="例：田中太郎", label_visibility="collapsed", key="input_pname")

    # === 参加可能時間設定（オプション）===
    st.markdown("### 2. 参加可能な時間（オプション）")
    st.caption("特に設定しなくてもOK。「平日は19時以降だけ」「土曜は午後だけ」など細かく指定したい場合に使ってください")

    time_options = [f"{h:02d}:{m:02d}" for h in range(24) for m in [0, 30]]
    default_start_idx = time_options.index(event['start_time'])
    default_end_idx = time_options.index(event['end_time'])

    with st.expander("⏰ 平日／土日の時間帯を設定する", expanded=False):
        st.markdown("**平日（月〜金）の参加可能時間**")
        c1, c2 = st.columns(2)
        with c1:
            wd_start = st.selectbox("平日 開始", time_options, index=default_start_idx, key="wd_start", label_visibility="collapsed")
        with c2:
            wd_end = st.selectbox("平日 終了", time_options, index=default_end_idx, key="wd_end", label_visibility="collapsed")

        st.markdown("**土日の参加可能時間**")
        c3, c4 = st.columns(2)
        with c3:
            we_start = st.selectbox("土日 開始", time_options, index=default_start_idx, key="we_start", label_visibility="collapsed")
        with c4:
            we_end = st.selectbox("土日 終了", time_options, index=default_end_idx, key="we_end", label_visibility="collapsed")

    with st.expander("❌ 「この時間は絶対NG」を追加する", expanded=False):
        st.caption("仕事の固定予定など、カレンダーに入れていなくても除外したい時間帯を追加できます")
        if "exclusions" not in st.session_state:
            st.session_state.exclusions = []

        for i, exc in enumerate(st.session_state.exclusions):
            cols = st.columns([2, 2, 1, 2, 1])
            with cols[0]:
                exc['day_type'] = st.selectbox("曜日", ["平日", "土日", "毎日"],
                                                index=["平日", "土日", "毎日"].index(exc.get('day_type', '平日')),
                                                key=f"exc_dt_{i}", label_visibility="collapsed")
            with cols[1]:
                exc['start'] = st.selectbox("開始", time_options,
                                             index=time_options.index(exc.get('start', '12:00')),
                                             key=f"exc_st_{i}", label_visibility="collapsed")
            with cols[2]:
                st.markdown("<div style='padding-top:8px;text-align:center'>〜</div>", unsafe_allow_html=True)
            with cols[3]:
                exc['end'] = st.selectbox("終了", time_options,
                                           index=time_options.index(exc.get('end', '13:00')),
                                           key=f"exc_en_{i}", label_visibility="collapsed")
            with cols[4]:
                if st.button("🗑", key=f"exc_del_{i}"):
                    st.session_state.exclusions.pop(i)
                    st.rerun()

        if st.button("+ 除外時間帯を追加"):
            st.session_state.exclusions.append({"day_type": "平日", "start": "12:00", "end": "13:00"})
            st.rerun()

    # === スクショ撮影のコツ ===
    st.markdown("### 3. カレンダーのスクショを送る")
    with st.container(border=True):
        st.markdown("##### 📸 スクショを撮るときのコツ")
        st.markdown(f"""
- **候補期間（{d_from.strftime('%Y年%m月%d日')}〜{d_to.strftime('%Y年%m月%d日')}）が全部映る** ように撮る
- **日付の数字** がはっきり見えるか確認（小さすぎるとAIが読めません）
- **時間軸** が見える表示（週間表示／日表示）がおすすめ。月表示だと時間が分からないので精度が下がります
- 期間が長い場合は **複数枚に分けて** 撮影してOK
- Google・Apple・Outlook・手帳の写真、何でもOK
""")

    uploaded_files = st.file_uploader(
        "画像をアップロード",
        type=["png", "jpg", "jpeg", "webp"],
        label_visibility="collapsed",
        accept_multiple_files=True,
        key="input_files",
    )

    st.caption("または ↓ クリップボードから貼り付け（スクショ直後に使えます）")
    pasted_images = st.session_state.get("pasted_images", [])
    try:
        from streamlit_paste_button import paste_image_button as pbutton
        paste_result = pbutton(label="📋 クリップボードから貼り付け", key="paste_btn")
        if paste_result.image_data is not None:
            pasted_images.append(paste_result.image_data)
            st.session_state.pasted_images = pasted_images
    except Exception:
        st.caption("（コピペ機能は読み込み中…）")

    if pasted_images:
        st.write(f"📋 貼り付け済み：{len(pasted_images)}枚")
        cols = st.columns(min(4, len(pasted_images)))
        for i, img in enumerate(pasted_images):
            with cols[i % len(cols)]:
                st.image(img, width=120)
        if st.button("貼り付けをクリア", key="clear_paste"):
            st.session_state.pasted_images = []
            st.rerun()

    n_uploaded = len(uploaded_files or [])
    n_pasted = len(pasted_images)
    n_images = n_uploaded + n_pasted

    if n_images > 0 and name:
        st.success(f"✅ 名前OK / 画像 {n_images}枚 セット完了。下のボタンで読み取り開始")
    elif not name and n_images > 0:
        st.warning("お名前を入力してください")
    elif name and n_images == 0:
        st.warning("カレンダーの画像を送ってください（アップロードまたは貼り付け）")

    if st.button("AIで読み取る", type="primary", use_container_width=True, disabled=(not name or n_images == 0)):
        import io
        all_images = []
        for f in (uploaded_files or []):
            all_images.append((f.read(), f.type or "image/png"))
        for img in pasted_images:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            all_images.append((buf.getvalue(), "image/png"))

        for img_bytes, _ in all_images:
            if len(img_bytes) > MAX_IMAGE_SIZE_MB * 1024 * 1024:
                st.error(f"画像サイズが大きすぎます（1枚あたり {MAX_IMAGE_SIZE_MB}MB以下にしてください）")
                return

        all_dates = set()
        visible_ranges = []
        is_mock_any = False
        progress = st.progress(0, text="AIが読み取り中...")
        for i, (img_bytes, mime) in enumerate(all_images):
            progress.progress((i) / max(1, n_images), text=f"AIが読み取り中... ({i+1}/{n_images}枚目)")
            try:
                dates, is_mock, vr = extract_available_dates(img_bytes, mime, event)
                all_dates.update(dates)
                if is_mock:
                    is_mock_any = True
                if vr and vr.get('from') and vr.get('to'):
                    visible_ranges.append(vr)
            except Exception as e:
                st.error(f"{i+1}枚目の読み取りでエラー: {e}")
                return
        progress.progress(1.0, text="完了！")

        valid = []
        for d_str in all_dates:
            try:
                d = datetime.strptime(d_str, '%Y-%m-%d').date()
                if d_from <= d <= d_to:
                    valid.append(d_str)
            except ValueError:
                continue
        valid = sorted(set(valid))

        st.session_state.participant_name = name
        st.session_state.p_dates = valid
        st.session_state.p_is_mock = is_mock_any
        st.session_state.p_visible_ranges = visible_ranges
        # 参加可能時間設定を保存
        st.session_state.p_settings = {
            "weekday_start": wd_start,
            "weekday_end": wd_end,
            "weekend_start": we_start,
            "weekend_end": we_end,
            "exclusions": list(st.session_state.get("exclusions", [])),
        }
        st.session_state.p_step = 2
        st.session_state.pasted_images = []
        st.rerun()

def participant_step2(event_id, event):
    st.markdown("### 4. 参加できる日時にチェック")
    st.caption("AIが候補期間の予定を読み取りました。下の日時のうち、**参加できる枠だけ** にチェックを残してください")

    if st.session_state.get("p_is_mock"):
        st.warning("⚠️ 開発モード：API キーが未設定のため、模擬データを表示しています（実際のスクショ内容は読み取られていません）")

    # === スクショカバレッジ不足チェック ===
    d_from = datetime.strptime(event['date_from'], '%Y-%m-%d').date()
    d_to = datetime.strptime(event['date_to'], '%Y-%m-%d').date()
    visible_ranges = st.session_state.get("p_visible_ranges", [])
    if visible_ranges:
        try:
            min_visible = min(datetime.strptime(vr['from'], '%Y-%m-%d').date() for vr in visible_ranges)
            max_visible = max(datetime.strptime(vr['to'], '%Y-%m-%d').date() for vr in visible_ranges)
            missing_before = (min_visible - d_from).days if min_visible > d_from else 0
            missing_after = (d_to - max_visible).days if max_visible < d_to else 0
            if missing_before > 0 or missing_after > 0:
                msg_parts = []
                if missing_before > 0:
                    msg_parts.append(f"**{d_from.strftime('%m/%d')}〜{(min_visible - timedelta(days=1)).strftime('%m/%d')}**（{missing_before}日分）")
                if missing_after > 0:
                    msg_parts.append(f"**{(max_visible + timedelta(days=1)).strftime('%m/%d')}〜{d_to.strftime('%m/%d')}**（{missing_after}日分）")
                st.warning(f"⚠️ アップロードされたスクショには {' と '.join(msg_parts)} が映っていない可能性があります。前のステップに戻ってスクショを追加することをおすすめします")
                if st.button("← 前のステップに戻る"):
                    for k in list(st.session_state.keys()):
                        if k.startswith("p_") or k.startswith("slot_") or k.startswith("date_"):
                            del st.session_state[k]
                    st.session_state.p_step = 1
                    st.rerun()
        except (KeyError, ValueError):
            pass

    n = len(st.session_state.p_dates)
    if n == 0:
        st.error("候補期間内に空き日が見つかりませんでした。スクショを撮り直してみてください")
        if st.button("← やり直す"):
            for k in list(st.session_state.keys()):
                if k.startswith("p_") or k.startswith("slot_") or k.startswith("date_"):
                    del st.session_state[k]
            st.rerun()
        return

    # 時間帯を所要時間単位でスロット化
    slots = generate_time_slots(
        event['start_time'], event['end_time'],
        event['duration_hours'], event['duration_minutes']
    )
    if not slots:
        st.error(
            "このイベントは「時間帯」より「所要時間」が長く設定されているため、空き枠を作れません。\n\n"
            f"- 時間帯: {event['start_time']} 〜 {event['end_time']}\n"
            f"- 所要時間: {event['duration_hours']}時間{event['duration_minutes']:02d}分\n\n"
            "イベント作成者に設定の見直しを依頼してください"
        )
        return

    # === 参加者設定によるスロットフィルタ ===
    settings = st.session_state.get("p_settings", {})
    filtered_dates = []
    filtered_slots_by_date = {}
    for d_str in st.session_state.p_dates:
        d = datetime.strptime(d_str, '%Y-%m-%d').date()
        is_weekend = d.weekday() in [5, 6]
        # 曜日タイプによる時間窓
        if is_weekend:
            win_s = settings.get("weekend_start", event['start_time'])
            win_e = settings.get("weekend_end", event['end_time'])
        else:
            win_s = settings.get("weekday_start", event['start_time'])
            win_e = settings.get("weekday_end", event['end_time'])

        valid_slots = []
        for s_st, s_en in slots:
            # 時間窓に収まっているか
            if s_st < win_s or s_en > win_e:
                continue
            # 除外時間帯と被っているか
            excluded = False
            for exc in settings.get("exclusions", []):
                dt = exc.get('day_type', '毎日')
                if dt == '平日' and is_weekend:
                    continue
                if dt == '土日' and not is_weekend:
                    continue
                # 重なり判定
                if s_st < exc['end'] and s_en > exc['start']:
                    excluded = True
                    break
            if excluded:
                continue
            valid_slots.append((s_st, s_en))

        if valid_slots:
            filtered_dates.append(d_str)
            filtered_slots_by_date[d_str] = valid_slots

    n_filtered = len(filtered_dates)
    if n_filtered == 0:
        st.error("参加可能時間／除外設定で全ての枠が除外されました。設定を見直してください")
        if st.button("← 前のステップに戻る"):
            for k in list(st.session_state.keys()):
                if k.startswith("p_") or k.startswith("slot_"):
                    del st.session_state[k]
            st.session_state.p_step = 1
            st.rerun()
        return

    st.success(f"✅ AI読み取り完了：**{n_filtered}日** に空きあり")

    # 初期化：p_selected_slots に "YYYY-MM-DD|HH:MM-HH:MM" 形式で保持
    if "p_selected_slots" not in st.session_state:
        initial = set()
        for d_str, valid_slots in filtered_slots_by_date.items():
            for s_st, s_en in valid_slots:
                initial.add(f"{d_str}|{s_st}-{s_en}")
        st.session_state.p_selected_slots = initial

    # 月ごとにグループ化
    by_month = defaultdict(list)
    for d_str in filtered_dates:
        d = datetime.strptime(d_str, '%Y-%m-%d').date()
        by_month[(d.year, d.month)].append(d)

    with st.container(border=True):
        for (year, month), dates in sorted(by_month.items()):
            st.markdown(f"**{year}年{month}月**")
            for d in dates:
                d_str = d.isoformat()
                st.markdown(f"📅 **{d.month}月{d.day}日（{WEEKDAY_JP[d.weekday()]}）**")
                for s_st, s_en in filtered_slots_by_date[d_str]:
                    slot_id = f"{d_str}|{s_st}-{s_en}"
                    key = f"slot_{slot_id}"
                    if key not in st.session_state:
                        st.session_state[key] = slot_id in st.session_state.p_selected_slots
                    checked = st.checkbox(
                        f"　{s_st} 〜 {s_en}",
                        key=key
                    )
                    if checked:
                        st.session_state.p_selected_slots.add(slot_id)
                    else:
                        st.session_state.p_selected_slots.discard(slot_id)
                st.write("")

    st.info("💡 予定を入れたくない日程があればチェックを外してください")

    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("やり直す"):
            for k in list(st.session_state.keys()):
                if k.startswith("p_") or k.startswith("date_") or k.startswith("slot_"):
                    del st.session_state[k]
            st.rerun()
    with col2:
        if st.button("送信する", type="primary", use_container_width=True):
            subs = load_submissions()
            event_subs = subs.get(event_id, [])
            if len(event_subs) >= MAX_PARTICIPANTS:
                st.error(f"参加者数の上限（{MAX_PARTICIPANTS}名）に達しました")
                return
            event_subs.append({
                "name": st.session_state.participant_name,
                "available_slots": sorted(list(st.session_state.p_selected_slots)),
                "submitted_at": datetime.now().isoformat(),
            })
            subs[event_id] = event_subs
            save_submissions(subs)
            st.session_state.p_step = 3
            st.rerun()

def participant_step3():
    st.balloons()
    st.markdown("# 🎉 送信できました！")
    st.write("主催者が日程を確定したら連絡が届きます。ご協力ありがとうございました")
    n = len(st.session_state.get("p_selected_slots", []))
    with st.container(border=True):
        st.markdown(f"""
**📤 送信した内容**

- お名前：{st.session_state.get('participant_name', '')}
- 参加可能枠：{n}枠
""")

# ===== 画面：管理（集計） =====
def show_admin_results(event_id, admin_token):
    events = load_events()
    if event_id not in events:
        st.error("このイベントは存在しないか、期限切れです")
        return
    event = events[event_id]
    if event.get("admin_token") != admin_token:
        st.error("管理URLが正しくありません")
        return

    subs = load_submissions().get(event_id, [])
    n_total = len(subs)

    st.markdown("# 📊 集計結果")
    st.caption(event['name'])

    col1, col2 = st.columns([3, 1])
    with col2:
        st.metric("人が送信済み", n_total)

    if n_total == 0:
        st.info("まだ送信がありません。参加者URLを配ってください")
        st.code(f"{get_base_url()}?event={event_id}", language=None)
        return

    # 集計：スロット単位でカウント（旧形式の available_dates も互換対応）
    slot_counts = Counter()
    for s in subs:
        slots_list = s.get('available_slots') or []
        if not slots_list and s.get('available_dates'):
            # 旧データ互換：date のみだったら時間帯全体を1スロットとして扱う
            for d in s['available_dates']:
                slots_list.append(f"{d}|{event['start_time']}-{event['end_time']}")
        for slot_id in slots_list:
            slot_counts[slot_id] += 1

    def parse_slot(slot_id):
        d_str, time_range = slot_id.split('|')
        s_st, s_en = time_range.split('-')
        return d_str, s_st, s_en

    # TOP3（スロット単位）
    st.markdown("### 🏆 おすすめ日程 TOP3")
    top3 = slot_counts.most_common(3)
    medals = ["🥇", "🥈", "🥉"]
    for i, (slot_id, count) in enumerate(top3):
        d_str, s_st, s_en = parse_slot(slot_id)
        d = datetime.strptime(d_str, '%Y-%m-%d').date()
        pct = int(count / n_total * 100)
        unavail = []
        for s in subs:
            slots_list = s.get('available_slots') or []
            if not slots_list and s.get('available_dates'):
                for dd in s['available_dates']:
                    slots_list.append(f"{dd}|{event['start_time']}-{event['end_time']}")
            if slot_id not in slots_list:
                unavail.append(s['name'])
        unavail_str = ""
        if unavail:
            head = "・".join(unavail[:2])
            tail = f"他{len(unavail)-2}名" if len(unavail) > 2 else ""
            unavail_str = f"（{head}{tail}が不可）"
        with st.container(border=True):
            st.markdown(f"### {medals[i]} {d.month}月{d.day}日（{WEEKDAY_JP[d.weekday()]}）{s_st}〜{s_en}")
            st.markdown(f"**{count}/{n_total}人 参加可能** {unavail_str} — **{pct}%**")

    # ヒートマップ：日付ごとに「その日のベストスロット人数」を表示
    st.markdown("### 🗓 全候補日のヒートマップ")
    st.caption("数字＝その日の最も人気な枠の参加可能人数")
    date_best = defaultdict(int)
    for slot_id, count in slot_counts.items():
        d_str, _, _ = parse_slot(slot_id)
        if count > date_best[d_str]:
            date_best[d_str] = count

    if date_best:
        sorted_dates = sorted(date_best.keys())
        rows = [sorted_dates[i:i+7] for i in range(0, len(sorted_dates), 7)]
        for row in rows:
            cols = st.columns(7)
            for i, d_str in enumerate(row):
                d = datetime.strptime(d_str, '%Y-%m-%d').date()
                count = date_best[d_str]
                ratio = count / n_total
                if ratio >= 0.9:
                    bg = "#10b981"; fg = "white"
                elif ratio >= 0.7:
                    bg = "#34d399"; fg = "white"
                elif ratio >= 0.5:
                    bg = "#a7f3d0"; fg = "#065f46"
                elif ratio >= 0.3:
                    bg = "#d1fae5"; fg = "#065f46"
                else:
                    bg = "#f3f4f6"; fg = "#6b7280"
                with cols[i]:
                    st.markdown(
                        f"<div style='background:{bg};color:{fg};border-radius:8px;padding:8px;text-align:center;font-size:12px;font-weight:600;margin-bottom:4px;'>"
                        f"{d.month}/{d.day}<br>{count}人</div>",
                        unsafe_allow_html=True
                    )

    # 送信者一覧
    st.markdown("### 👥 送信してくれた人")
    for s in subs:
        n_slots = len(s.get('available_slots') or s.get('available_dates') or [])
        with st.container(border=True):
            col_a, col_b = st.columns([3, 1])
            with col_a:
                st.write(f"**{s['name']}**")
            with col_b:
                st.write(f"{n_slots}枠 参加可能")

# ===== ルーティング =====
def main():
    st.set_page_config(page_title="スクショで簡単！ 予定調整", page_icon="📸", layout="centered")
    inject_css()

    params = st.query_params
    event_id = params.get("event")
    admin_token = params.get("admin")

    if event_id and admin_token:
        show_admin_results(event_id, admin_token)
    elif event_id:
        show_participant(event_id)
    else:
        show_create_event()

main()
