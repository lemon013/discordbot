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

`./ai chat 質問` またはBotへのメンションで応答します。`./ai model`、`./ai prompt`、`./ai reasoning` は「サーバー管理」権限で変更できます。モデルの利用可否・無料枠はGoogle側の設定に従います。

Discord Developer PortalでMessage Content Intentを有効にしてください。起動ファイルは従来どおり `cloud_main.py` です。

AI設定は `Discord-GameBot/ai_settings.json` に保存します。MWSの再デプロイでファイルが置き換わる場合に備え、このファイルをバックアップしてください。会話履歴はメモリ内のみで再起動時に消えます。

詳しいコマンド・思考設定は [AI_README.md](Discord-GameBot/AI_README.md) を参照してください。
