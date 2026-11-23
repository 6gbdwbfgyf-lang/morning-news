# ☀️ 朝のニュース

毎朝7時(日本時間)までに、前日から今朝にかけての主要ニュースを約10件ピックアップし、AIが要約して表示するWebアプリです。

## しくみ

1. **GitHub Actions** が毎朝 6:40(JST)に自動起動(`.github/workflows/daily-digest.yml`)
2. `scripts/build_digest.py` が NHK・Yahoo!ニュースのRSSから直近30時間の記事を集める
3. AIが約10件を選んで要約する(下表)
4. 結果を `docs/data/YYYY-MM-DD.json` に保存してコミット
5. **GitHub Pages** が `docs/` をそのままWebページとして公開(過去の日付も選べます)

サーバーは不要です。公開リポジトリなら GitHub Actions・GitHub Pages は無料です。

### 要約のしかた(自動で切り替わります)

| モード | 条件 | 費用 | 内容 |
|---|---|---|---|
| **無料AI要約(既定)** | 何もしなくてOK | 無料 | GitHub Models の無料枠を使い、AIが候補25件から約10件を選んで要約と「なぜ重要か」を書きます。GitHub Actions に最初から付いている `GITHUB_TOKEN` を使うので登録やAPIキーは不要です。 |
| AIなし | 上の無料枠が使えなかった日 | 無料 | 主要ニュースをルールで約10件選び、配信元の概要文をそのまま表示します。ページが更新されない日はありません。 |
| Claude 要約 | `ANTHROPIC_API_KEY` を登録 | 1日数円〜十数円 | Claude が全記事から選んで要約。いちばん高品質です。 |

GitHub Models の無料枠は「試作向け」で、1日のリクエスト数や1回の文字数に上限があります(このアプリは1日1回・数千トークンなので通常は収まります)。使うモデルはリポジトリの変数 `GITHUB_MODELS_MODEL` で変えられます(既定 `openai/gpt-4.1-mini`)。

## セットアップ

1. このフォルダをGitHubの**公開(Public)リポジトリ**にpush(非公開だとPagesが有料プラン限定のため)
2. (Claude 要約を使う場合のみ)リポジトリの **Settings → Secrets and variables → Actions** で `ANTHROPIC_API_KEY` を登録
   (キーは https://console.anthropic.com/ で発行)
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
