# PCのチャット連携とMWSへの成績自動同期

## 仕組み

MWSでは従来どおり cloud_main.py を動かします。
PCでは pc_main.py を動かし、開いたMinecraftワールドと接続します。
チャットはPC専用Botと指定のチャットチャンネル間で連携します。
ゲームBotとチャットBotは別のトークンを使います。
成績はPCから非公開Discordチャンネルに添付送信し、MWSが取り込みます。
外部ポート公開やルーター設定は不要です。
PCを閉じている間もクラウドのゲームと最終同期済み成績の表示は利用できます。
チャットと最新成績の取得にはPC・ワールド・PC側プログラムの起動が必要です。

## 1. Discord側の準備

1. Discord Developer Portalで、チャット連携用にもう1つApplication/Botを作成します。
2. BotページでMessage Content Intentを有効にします。
3. この専用Botを同じDiscordサーバーに招待します。
   チャットチャンネルで「チャンネルを見る」「メッセージを送信」「埋め込みリンク」
   「メッセージ履歴を読む」の権限を付けます。管理者権限は不要です。
4. 成績転送専用のテキストチャンネルを別に作ります。
   @everyoneの「チャンネルを見る」を無効にし、管理者と2つのBotのみ閲覧可能にします。
   PC専用Botには閲覧・送信・ファイル添付、クラウドBotには閲覧・履歴を読む権限を付けます。
5. クラウドBotもMessage Content Intentを有効にしてください。
6. Discordの開発者モードでサーバーID・各チャンネルID・PC専用BotのユーザーIDをコピーします。
   MC_SYNC_SENDER_ID はアプリケーション名やトークンではなくPC専用Botの数値ユーザーIDです。

## 2. PC側の準備

GitHubの最新コードをPCへ取得し、既存プロジェクトと同じフォルダ構成で配置します。
既存の Minecraft-Discord-Bridge/updater.exe は手元に残してください（GitHubには不要）。
Bunをインストール済みならupdater.exeなしでも起動できます。
PC-Bridge/.env.example をコピーして PC-Bridge/.env を作ります。

```dotenv
DISCORD_TOKEN=PC専用Botのトークン
GUILD_ID=DiscordサーバーID
CHANNEL_ID=チャット連携チャンネルID
MC_SYNC_CHANNEL_ID=成績転送専用チャンネルID
MC_SYNC_SECRET=自分で生成した32文字以上のランダムな共有キー
```

共有キーは例えばPCのPythonで生成できます。生成結果はPCとMWSだけに設定してください。

```bat
py -3 -c "import secrets; print(secrets.token_hex(32))"
```

.env やトークン・共有キー・成績データはGitHubや公開チャットに載せません。
必要な依存ライブラリがない場合は `py -3 -m pip install -r requirements.txt` を実行します。
START_PC_BRIDGE.bat または `py -3 pc_main.py` で起動します。
旧 main.py や旧Bridgeは同時起動しないでください（3063ポートが競合します）。
PC専用の設定・保存先は PC-Bridge フォルダです。元のBridge設定は変更しません。
初回のみ元のMinecraft-Discord-Bridge/.discord-mcbe/data.jsonから成績を引き継ぎます。
元のデータがない場合は、以下の登録を行ってください。

## 3. MWS側の環境変数

従来の DISCORD_BOT_TOKEN はゲームBotのままにします。
以下を追加して再起動します。

```dotenv
MC_SYNC_CHANNEL_ID=PCと同じ成績転送専用チャンネルID
MC_SYNC_SENDER_ID=PC専用BotのユーザーID
MC_SYNC_SECRET=PCと同じ共有キー
```

既存のPLAYER_STATUS_FILE設定があれば、そのパスに書き込みます。
未指定ならMinecraft-Discord-Bridge/.discord-mcbe/data.jsonに保存します。
MWSで書き込み可能な永続保存先が指定できる場合は、そのパスを設定してください。
再起動時には成績転送チャンネルの直近50件から最新の署名付きデータを取り込めます。
専用チャンネルは成績転送だけに使い、最新の添付を削除しないでください。
ファイル転送はDiscordを経由するため、成績データはDiscordにも保存されます。

## 4. Minecraftの接続と保存対象

PCでワールドを開き、PC版Bridgeのログに出る /connect コマンドを実行します。
基本は `/connect localhost:3063` です。追加文字列が案内されたらログに従ってください。
管理者の yumokiti4325 / GreenView777 が以下を登録できます。

| ゲーム | ワールド内で実行するコマンド |
|---|---|
| unlimited | !savescore up / !savescore wingame / !savescore killCount / !savescore deathCount |
| MurderMysteryRe:MASTER | !savescore Murder / !savescore Crew / !savescore wingame |
| MINIGAMER | !savescore MINIGAMER_NEO（別にrocalを使う場合は !savescore rocal も登録） |

各コマンドは1つずつ実行します。!savelistで登録済みの対象を確認できます。
ワールド名にunlimited / MurderMystery / 人狼 / MINIGAMERが含まれる場合に分類します。
MINIGAMER_NEO登録ワールドもMINIGAMERとして扱います。

## 5. 確認

1. ゲーム内チャットがチャットチャンネルに届くか、逆方向も確認します。
2. PCログに Scores sent to cloud sync channel が出るか確認します。
3. MWSログに Minecraft scores synchronized from PC が出るか確認します。
4. /player status と /vs で数値と記録更新時刻を確認します。

成績の取得は約10秒間隔、送信は約60秒間隔、クラウドの確認は約30秒間隔です。
通常は変更から約100秒以内が目安ですが、通信遅延・レート制限時は遅れます。
同期対象は登録した全ワールドの成績です。クラウドの手動データ編集は次回同期で置き換わります。
PC時計が大きくずれている場合はWindowsの時刻同期を行ってください。
7MBを超える成績データは送信せずエラーにします。
署名・送信元・チャンネル・時系列を検証し、不正なデータを取り込みません。

## 稼働の限界

ゲームの進行状態は従来どおりメモリ上のため、MWS再起動で終了します。
MWS側の利用期限や停止にも影響を受けます。自動延長は行いません。
この実装は自動テスト済みですが、2つのBotと実際のワールドを接続した確認は設定後に必要です。
