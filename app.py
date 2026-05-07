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

【出力形式】JSONのみ、説明文は一切なし:
{{"available_dates": ["YYYY-MM-DD", "YYYY-MM-DD", ...]}}"""

def parse_json_response(text):
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data.get("available_dates", [])
    except json.JSONDecodeError:
        return []

def extract_with_gemini(image_bytes, mime_type, event):
    from google import genai
    from google.genai import types
    import time

    client = genai.Client(api_key=get_gemini_key())
    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    config = types.GenerateContentConfig(response_mime_type="application/json")
    contents = [image_part, build_extraction_prompt(event)]

    # 混雑時の対策：複数モデル × リトライ
    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-flash-lite"]
    last_err = None
    for model in models_to_try:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model, contents=contents, config=config
                )
                return parse_json_response(response.text)
            except Exception as e:
                msg = str(e)
                last_err = e
                # 503/429 はリトライ、それ以外はモデル切替
                if "503" in msg or "429" in msg or "UNAVAILABLE" in msg or "RESOURCE_EXHAUSTED" in msg:
                    time.sleep(2 ** attempt)  # 1, 2, 4秒
                    continue
                break  # 他のエラーは即座にモデル切替
    raise last_err

def extract_with_claude(image_bytes, mime_type, event):
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
    return parse_json_response(response.content[0].text)

def extract_available_dates(image_bytes, mime_type, event):
    """空き日付のリストを返す。第2戻り値はモックフラグ"""
    # 優先順：Gemini（無料枠あり）→ Claude → モック
    if get_gemini_key():
        try:
            return extract_with_gemini(image_bytes, mime_type, event), False
        except ImportError:
            st.warning("google-genai ライブラリが未インストールです。pip install google-genai を実行してください")
            return mock_extract(event), True
        except Exception as e:
            msg = str(e)
            if "503" in msg or "UNAVAILABLE" in msg:
                st.error("Gemini が一時的に混雑しています。1〜2分待ってからもう一度「AIで読み取る」を押してください。\n\n※ 解消しない場合は Anthropic API キーを設定すると Claude に自動切替されます")
            elif "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                st.error("Gemini の無料枠の利用上限に達しました（1分15回・1日1500回）。少し時間をおいてからもう一度お試しください")
            else:
                st.error(f"Gemini API エラー: {msg}")
            return [], False

    if get_anthropic_key():
        try:
            return extract_with_claude(image_bytes, mime_type, event), False
        except ImportError:
            return mock_extract(event), True
        except Exception as e:
            st.error(f"Claude API エラー: {e}")
            return [], False

    return mock_extract(event), True

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
    st.markdown("### 1. お名前")
    name = st.text_input("お名前", placeholder="例：つる", label_visibility="collapsed", key="input_pname")

    st.markdown("### 2. カレンダーのスクショを送る")
    d_from = datetime.strptime(event['date_from'], '%Y-%m-%d').date()
    d_to = datetime.strptime(event['date_to'], '%Y-%m-%d').date()
    st.caption(
        f"候補期間（{d_from.strftime('%Y年%m月%d日')} 〜 {d_to.strftime('%Y年%m月%d日')}）が映っているスクショを送ってください。"
        f"\n\n📌 **週間表示** が読み取り精度高くておすすめです。"
        f"\n📌 複数枚OK／ドラッグ＆ドロップ可／クリップボードからコピペ可"
        f"\n📌 Google・Apple・Outlook・手帳の写真、なんでもOK"
    )

    uploaded_files = st.file_uploader(
        "画像をアップロード",
        type=["png", "jpg", "jpeg", "webp"],
        label_visibility="collapsed",
        accept_multiple_files=True,
        key="input_files",
    )

    # クリップボードから貼り付け
    st.caption("または ↓ クリップボードから貼り付け（スクショを撮った直後に使えます）")
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

    st.markdown(
        '<div class="privacy-note">🔒 <b>プライバシー</b>：画像はAIが日付を読み取った後すぐ破棄され、保存されません。主催者にも画像は届きません（日付の文字情報だけが届きます）</div>',
        unsafe_allow_html=True,
    )

    if n_images > 0 and name:
        st.success(f"✅ 名前OK / 画像 {n_images}枚 セット完了。下のボタンで読み取り開始")
    elif not name and n_images > 0:
        st.warning("お名前を入力してください")
    elif name and n_images == 0:
        st.warning("カレンダーの画像を送ってください（アップロードまたは貼り付け）")

    if st.button("AIで読み取る", type="primary", use_container_width=True, disabled=(not name or n_images == 0)):
        # 全画像のバイト列を集める
        import io
        all_images = []
        for f in (uploaded_files or []):
            all_images.append((f.read(), f.type or "image/png"))
        for img in pasted_images:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            all_images.append((buf.getvalue(), "image/png"))

        # サイズチェック
        for img_bytes, _ in all_images:
            if len(img_bytes) > MAX_IMAGE_SIZE_MB * 1024 * 1024:
                st.error(f"画像サイズが大きすぎます（1枚あたり {MAX_IMAGE_SIZE_MB}MB以下にしてください）")
                return

        # 全画像を順に処理して結果を統合
        all_dates = set()
        is_mock_any = False
        progress = st.progress(0, text="AIが読み取り中...")
        for i, (img_bytes, mime) in enumerate(all_images):
            progress.progress((i) / max(1, n_images), text=f"AIが読み取り中... ({i+1}/{n_images}枚目)")
            try:
                dates, is_mock = extract_available_dates(img_bytes, mime, event)
                all_dates.update(dates)
                if is_mock:
                    is_mock_any = True
            except Exception as e:
                st.error(f"{i+1}枚目の読み取りでエラー: {e}")
                return
        progress.progress(1.0, text="完了！")

        # 候補期間でフィルタ
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
        st.session_state.p_selected = set(valid)
        st.session_state.p_step = 2
        st.session_state.pasted_images = []
        st.rerun()

def participant_step2(event_id, event):
    st.markdown("### 3. 参加できる日にチェック")
    st.caption("AIが候補期間の予定を読み取りました。下の日付のうち、**参加できる日だけ**にチェックを入れてください")

    if st.session_state.get("p_is_mock"):
        st.warning("⚠️ 開発モード：API キーが未設定のため、模擬データを表示しています（実際のスクショ内容は読み取られていません）")

    n = len(st.session_state.p_dates)
    if n == 0:
        st.error("候補期間内に空き日が見つかりませんでした")
        if st.button("やり直す"):
            for k in ["p_step", "p_dates", "p_selected"]:
                st.session_state.pop(k, None)
            st.rerun()
        return

    # 時間帯を所要時間単位でスロット化
    slots = generate_time_slots(
        event['start_time'], event['end_time'],
        event['duration_hours'], event['duration_minutes']
    )
    if not slots:
        st.error("時間帯と所要時間の設定に問題があります（イベント作成者にご連絡ください）")
        return

    n_slots_per_date = len(slots)
    st.success(f"✅ AI読み取り完了：**{n}日** に空きあり（1日あたり {n_slots_per_date} 枠）")

    # 初期化：p_selected_slots に "YYYY-MM-DD|HH:MM-HH:MM" 形式で保持
    if "p_selected_slots" not in st.session_state:
        initial = set()
        for d_str in st.session_state.p_dates:
            for s_st, s_en in slots:
                initial.add(f"{d_str}|{s_st}-{s_en}")
        st.session_state.p_selected_slots = initial

    # 月ごとにグループ化
    by_month = defaultdict(list)
    for d_str in st.session_state.p_dates:
        d = datetime.strptime(d_str, '%Y-%m-%d').date()
        by_month[(d.year, d.month)].append(d)

    with st.container(border=True):
        for (year, month), dates in sorted(by_month.items()):
            st.markdown(f"**{year}年{month}月**")
            for d in dates:
                d_str = d.isoformat()
                st.markdown(f"📅 **{d.month}月{d.day}日（{WEEKDAY_JP[d.weekday()]}）**")
                for s_st, s_en in slots:
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
                st.write("")  # 日付間のスペース

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
