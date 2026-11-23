# ☀️ 朝のニュース

毎朝7時(日本時間)までに、前日から今朝にかけての主要ニュースを約10件ピックアップし、AIが要約して表示するWebアプリです。

## しくみ

1. **GitHub Actions** が毎朝 6:40(JST)に自動起動(`.github/workflows/daily-digest.yml`)
2. `scripts/build_digest.py` が Yahoo!ニュースのRSSから直近30時間の記事を集める
3. 約10件を選んで要約する(下表)
4. 結果を `docs/data/YYYY-MM-DD.json` に保存してコミット
5. **GitHub Pages** が `docs/` をそのままWebページとして公開(過去の日付も選べます)

サーバーは不要です。公開リポジトリなら GitHub Actions・GitHub Pages は無料です。

### 要約のしかた(自動で切り替わります)

| モード | 条件 | 費用 | 内容 |
|---|---|---|---|
| AIなし | 何もしない | 無料 | 主要ニュースをルールで約10件選び、記事ページの概要文をそのまま表示 |
| **無料AI要約** | `GEMINI_API_KEY` を登録 | 無料 | Google Gemini の無料枠で、候補30件から約10件を選んで要約と「なぜ重要か」を作成 |
| Claude 要約 | `ANTHROPIC_API_KEY` を登録 | 1日数円〜十数円 | Claude が全記事から選んで要約。いちばん高品質 |

Gemini の無料枠には1日の回数上限があり(このアプリは1日1回なので通常は収まります)、無料枠では送った内容がGoogleのサービス改善に使われることがあります(ニュースは公開情報なので実害は小さい想定)。失敗した日は自動で「AIなし」になり、更新が止まることはありません。モデルはリポジトリの変数 `GEMINI_MODEL` で固定もできます。

ニュースソースは Yahoo!ニュースのトピックスRSS です(NHKの旧RSSは2026年8月で更新停止)。

## セットアップ

1. このフォルダをGitHubの**公開(Public)リポジトリ**にpush(非公開だとPagesが有料プラン限定のため)
2. (AI要約を使う場合)リポジトリの **Settings → Secrets and variables → Actions** で `GEMINI_API_KEY`(https://aistudio.google.com/apikey で無料発行)または `ANTHROPIC_API_KEY` を登録
3. **Settings → Pages** で「Deploy from a branch」→ ブランチ `main`、フォルダ `/docs` を選択
4. **Actions → 朝のニュースを作る → Run workflow** で一度手動実行して動作確認

※ GitHub の仕様で、60日間リポジトリに動きがないと定時実行が止まることがあります。このアプリは毎日自分でコミットするので通常は止まりません。

## カスタマイズ

- ニュースソース: `scripts/build_digest.py` の `FEEDS`
- 件数: `TARGET_COUNT`
- 実行時刻: workflow の `cron`(UTC表記。JST − 9時間)

## ローカルで試す

```bash
pip install -r requirements.txt
python scripts/build_digest.py --dry-run          # 記事の取得だけ確認
ANTHROPIC_API_KEY=sk-... python scripts/build_digest.py
cd docs && python -m http.server                  # http://localhost:8000 で表示
```
