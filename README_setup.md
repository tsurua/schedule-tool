# スケジュール調整ツール — 本番デプロイ手順書

つるさんが手を動かすのは **合計1時間程度・1回限り** です。各ステップの所要時間を書いておくので、まとまった時間に一気に進めるのがおすすめです。

---

## このツールでできること

- 主催者は「イベント名・候補期間・時間帯・所要時間」を入力 → 参加者URLと管理URLが発行される
- 参加者はカレンダーのスクショをアップロード → AIが空き日を抽出 → 確認して送信
- 主催者は管理URLで集計結果（TOP3、ヒートマップ、送信済み一覧）を見る
- 画像は処理後すぐ破棄、主催者にも届かない

---

## 必要なアカウント（事前準備）

1. **GitHub** アカウント（無料）
2. **Streamlit Community Cloud** アカウント（無料、GitHub 連携でログイン）
3. **Anthropic** アカウント（API 利用・$5チャージ程度）

すでにあれば飛ばしてOK。

---

## ステップ1：API キーの発行

このツールは2種類のAIから選べます。**まずは無料の Gemini で試す** のがおすすめ。

### 推奨：Gemini API（無料）

1. https://aistudio.google.com/apikey にアクセス（Googleアカウントでログイン）
2. 「**Create API key**」を押す
3. 表示されたキー（`AIzaSy...` で始まる文字列）を **コピーして安全な場所にメモ**
4. 完了。クレカ登録不要

> Gemini の無料枠は **1日1500リクエスト・1分15リクエスト** まで。読書会10〜20人なら余裕です。

### あとから：Anthropic API（有料・$5チャージで実用可）

無料枠を使い切りそうな時、または Claude の方が精度が良いと判断した時に切り替えます。

1. https://console.anthropic.com にアクセスしてアカウント作成
2. 左メニュー「**Plans & Billing**」→ クレカ登録 → **$5 をチャージ**
3. 左メニュー「**Limits**」→ **Monthly spend limit** を **$5** に設定（上限ロック・超重要）
4. 左メニュー「**API keys**」→「Create Key」→ 名前を `schedule-tool` などに → **キーをコピーして安全な場所にメモ**
   - キーは `sk-ant-api03-...` で始まる長い文字列
   - 一度しか表示されないので必ずコピー

> ⚠️ Step 3 の月額上限を必ず設定してください。これで万一悪用されても請求は最大 $5 で打ち止めになります。

---

## ステップ2：GitHub にコードを置く（10分）

1. https://github.com/new で新しいリポジトリを作成
   - リポジトリ名：`schedule-tool`（任意）
   - **Public** にする（Streamlit Cloud 無料枠の条件）
   - 「Create repository」を押す
2. 自分のPCの `G:\AI\schedule-tool\` フォルダ内のファイルをすべて GitHub にアップロード
   - 簡単な方法：GitHub 画面の「uploading an existing file」リンクから、以下のファイルをドラッグ＆ドロップ
     - `app.py`
     - `requirements.txt`
     - `.streamlit/config.toml`（フォルダごと）
     - `.gitignore`
   - **`data/` フォルダは絶対にアップしないでください**（テスト中の中身が公開されます）
3. 「Commit changes」を押す

---

## ステップ3：Streamlit Cloud にデプロイ（10分）

1. https://share.streamlit.io にアクセス → GitHub でログイン
2. 「**New app**」を押す
3. 設定：
   - **Repository**：`<あなたのユーザー名>/schedule-tool`
   - **Branch**：`main`
   - **Main file path**：`app.py`
   - **App URL**：好きな名前（例：`my-schedule-tool`）
4. 「**Advanced settings**」を開く →「**Secrets**」欄に以下を貼る：

   **Gemini を使う場合（推奨・無料）：**
   ```toml
   GEMINI_API_KEY = "AIzaSy..."
   BASE_URL = "https://my-schedule-tool.streamlit.app/"
   ```

   **Anthropic Claude を使う場合：**
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-api03-..."
   BASE_URL = "https://my-schedule-tool.streamlit.app/"
   ```

   - `BASE_URL` は今設定した App URL（最後に `/` をつける）
   - 両方書いた場合は **Gemini が優先** されます。Gemini枠を使い切ったら Gemini の行を削除すれば Claude に切り替わります
5. 「**Deploy**」を押す → 2〜3分で起動

---

## ステップ4：動作確認（5分）

1. 起動した URL（例：`https://my-schedule-tool.streamlit.app/`）にアクセス
2. テスト用イベントを作る
3. 発行された参加者URLを別タブで開いて、自分のカレンダーのスクショで送信してみる
4. 管理URLで集計が見られるか確認

OKなら本番運用開始です 🎉

---

## 使い方（運用フロー）

### イベントを作る側（つるさん）

1. メインURL（`https://my-schedule-tool.streamlit.app/`）を開く
2. イベント情報を入力 → URL発行
3. **参加者URL** を LINE グループ等で配布
4. **管理URL** をブックマーク（自分専用）
5. 集まった頃に管理URLで集計を見る → 日程確定

### 参加する側（メンバー）

1. 配られた参加者URLを開く
2. 名前を入れる
3. カレンダーのスクショを上げる → AI読み取り
4. 内容を確認して送信

---

## 制限・注意事項

### 安全装置（実装済み）

- **月額API上限 $5**：Anthropic 側で固定（ステップ1で設定）
- **1イベント20名まで**：システム側で制限
- **画像5MBまで**：システム側で制限
- **画像は保存されない**：処理後すぐ破棄

### データ保管について

現在の構成では、参加者の送信データは **Streamlit Cloud のサーバー上のファイル** に保存されます。

- 通常運用では**問題なく数日〜数週間は保持**されます
- ただし、以下の状況でデータが消える可能性があります：
  - コードを更新して再デプロイした時
  - アプリが長期間（7日以上）アクセスがなく休眠して、再起動された時
- **重要なイベントの場合は、確定後すぐに集計画面のスクショを撮っておく** ことをおすすめします

データを永続的に残したい場合は、Supabase など外部DBへの切り替えが必要です。必要になったら声をかけてください、追加実装します。

### コスト

- **Streamlit Cloud**：完全無料（クレカ不要）
- **Gemini API（無料枠）**：1日1500リクエストまで完全無料 → 読書会用途ならこれで十分
- **Anthropic API（切り替えた場合）**：1人スクショ読み取りで約 $0.01〜0.03（数円程度）。読書会10人なら 1イベントあたり 100円前後

---

## トラブル時

### 「APIキーが間違っています」と出る
→ Streamlit Cloud のSecretsを再確認。`sk-ant-` から始まる正しいキーが入っているか。

### 「画像から日付が見つからない」
→ スクショに候補期間が映っているか確認。映っていれば再アップロードしてみる。

### アプリが起動しない
→ Streamlit Cloud の管理画面で「Manage app」→「Logs」を見る。エラーメッセージをコピーして相談してください。

---

## ファイル構成（参考）

```
schedule-tool/
├── app.py                     ← メインアプリ
├── requirements.txt           ← 必要ライブラリ
├── .streamlit/
│   └── config.toml           ← テーマ設定
├── .gitignore                ← Gitに上げないファイル
├── data/                     ← ローカル動作用（GitHubには上げない）
└── README_setup.md           ← このファイル
```
