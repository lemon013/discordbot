# MWSでのクラウド起動

MWSはPythonで cloud_main.py を起動してください。DISCORD_BOT_TOKEN はゲームBotのトークンです。
ゲーム、/player status、/vs はクラウドで利用できます。

## PCのチャット連携・成績自動同期

設定手順は [PC_CLOUD_SETUP.md](PC_CLOUD_SETUP.md) を参照してください。
PC側の専用Botと署名付きDiscord添付を経由して成績を同期します。
MWS側に MC_SYNC_CHANNEL_ID / MC_SYNC_SENDER_ID / MC_SYNC_SECRET を設定して再起動します。
未設定なら従来どおり保存済み成績だけを表示します。
PCを閉じている間もクラウドのゲームと最後に同期した成績を確認できます。
PCとワールドが起動中のときだけ、チャット連携と最新成績の取得が動きます。

参照・保存先は既定で Minecraft-Discord-Bridge/.discord-mcbe/data.json です。
PLAYER_STATUS_FILE で書き込み可能な永続保存先を変更できます。
.env・トークン・共有キー・成績ファイルはGitHubにアップロードしないでください。

クラウドサービス側の停止・再起動・利用期限には影響を受けます。

## Gemini AIチャット

GitHubの最新mainをMWSへ反映し、MWSの環境変数に `GEMINI_API_KEY` を設定してBotを再起動してください。APIキーはGitHubへ保存しないでください。

`/ai chat 質問` またはBotへのメンションで応答します。`/ai model`、`/ai prompt`、`/ai reasoning` は「サーバー管理」権限で変更できます。モデルの利用可否・無料枠はGoogle側の設定に従います。

Discord Developer PortalでMessage Content Intentを有効にしてください。起動ファイルは従来どおり `cloud_main.py` です。

AI設定は `Discord-GameBot/ai_settings.json` に保存します。MWSの再デプロイでファイルが置き換わる場合に備え、このファイルをバックアップしてください。会話履歴はメモリ内のみで再起動時に消えます。

詳しいコマンド・思考設定は [AI_README.md](Discord-GameBot/AI_README.md) を参照してください。

## テキスト・VCのレベリング

最新版を取り込んで再起動すると、テキスト発言と2人以上の通常VC参加でXPが貯まります。`/rank`、`/leaderboard` で確認し、サーバー管理者は `/level configure` で獲得量や必要XPを変更できます。

XPは既定で `Discord-GameBot/levels.sqlite3` に保存します。再デプロイで作業フォルダーが消える場合は、MWSの永続保存領域を `LEVEL_DB_PATH` 環境変数で指定してください。既存データを移動する場合はBot停止中にファイルを移してください。

詳しい条件と設定は [LEVELING_README.md](Discord-GameBot/LEVELING_README.md) を参照してください。

## 💎ジュエル・カジノ

最新版を取り込んで再起動すると、`/casino slot`・`/casino coinflip`・`/casino dice` で遊べます。`/jewel balance` で残高、`/jewel daily` で1日1回の報酬、`/jewel history` で最近の履歴を確認できます。初期残高は0ジュエル、デイリーは日本時間0時更新で500ジュエルです。

保存先は `CASINO_DB_PATH`、未設定なら `LEVEL_DB_PATH` と同じフォルダーの `casino.sqlite3`、どちらも未設定なら `Discord-GameBot/casino.sqlite3` です。MWSで保持される保存場所を利用し、保存先変更・バックアップはBot停止中に行ってください。

詳しい配当とコマンドは [CASINO_README.md](Discord-GameBot/CASINO_README.md) を参照してください。

`/gift member:相手 amount:数` でジュエルを送れます。初期残高0は新規ユーザーに適用され、既存残高は維持します。
