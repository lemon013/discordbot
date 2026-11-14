# MWSでのクラウド起動

1. このプロジェクトのフォルダ構成を保ったままMWSへアップロードします。
2. Pythonを選び、起動ファイルを cloud_main.py に設定します。
   ルートの mws.config にも main=cloud_main.py を指定しています。
3. 環境変数 DISCORD_BOT_TOKEN にBotのトークンを設定します。
   必要に応じて TEST_GUILD_ID にDiscordサーバーIDを指定します。
4. 元の main.py / Bridge をクラウドで起動している場合は停止し、クラウド版を起動します。
   同じBotトークンを使うPC側の旧Botも同時起動しないでください。

利用可能: /wordwolfex、/8192game、/player status、/vs。
/link と /delete はクラウド版では利用不可のメッセージを返します。
Minecraftチャット連携やワールドへの接続は行いません。Bunは不要です。

## 成績データ

参照先: Minecraft-Discord-Bridge/.discord-mcbe/data.json
アップロード時に .discord-mcbe フォルダも含めてください。
このファイルは空のデータで上書きしないでください。
別の場所を使う場合は PLAYER_STATUS_FILE にファイルパスを指定できます。

クラウドは保存済みデータのみ表示します。現在のワールドから自動取得しません。
PC側で最新データを取得した後、この data.json をMWS側の同じ場所へ
アップロードして置き換えると、次回のコマンドから反映されます。
PCからの自動同期は含んでいません。

クラウドサービス側の停止・再起動・利用期限には影響を受けます。
