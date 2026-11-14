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
