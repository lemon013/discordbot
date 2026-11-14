# Minecraft–Discord連携 + DiscordゲームBot（Windows）

このフォルダーの **`main.py` だけで Minecraft Discord Bridge と Discord GameBot の両方を起動・監視**できます。通常は `main.py` をダブルクリックするか、次のコマンドで起動してください。

```bat
py -3 main.py
```

`START_ALL.bat` も互換用に残してありますが、内部では `main.py` を呼び出します。BridgeとGameBotを別々の起動ファイルから同時に起動しないでください。

## 初回設定

1. `main.py` を起動します。
2. 初回は `Discord-GameBot\.env` が自動作成され、メモ帳で開きます。`DISCORD_BOT_TOKEN=` にゲームBot用トークンを設定して保存してください。
3. Bridgeの初回設定が必要な場合は、起動後のBridgeの案内に従ってください。
4. 以後は `main.py` を起動するだけです。Python仮想環境の作成と必要ライブラリのインストールも自動で行います。

Minecraft連携BotとゲームBotは同じDiscord Botトークンでは同時に動かせません。Discord Developer Portalで別々のBotを作成し、それぞれを同じDiscordサーバーへ招待してください。

## 起動・終了

- `main.py` がBridgeとGameBotを1つのコンソールで起動し、ログに `[Bridge]` / `[GameBot]` の接頭辞を付けて表示します。
- 終了するときはコンソールで `Ctrl+C` を押してください。起動した子プロセスだけを終了します。
- Bridgeのポート `3063` または `23191` が既に使用中の場合、二重起動を避けるため新しいBridgeは起動せず、GameBotの起動を続けます。既存Bridgeを確認し、不要なBridgeだけを通常の方法で終了してから再実行してください。無関係なプロセスを自動終了することはありません。

## プレイヤーステータスの保存設定

Minecraft内で、`yumokiti4325` または `GreenView777` が次のコマンドを実行できます。管理コマンドはDiscordへ転送されません。

- `!savescore <scoreboard>`: 今いるワールドと指定スコアボードを保存対象に登録します。ワールド名に `Unlimited` が含まれる場合は `unlimited`、`人狼` が含まれる場合は `MurderMystery`、スコアボード名に `MINIGAMER_NEO` が含まれる場合（同じワールド内の関連スコアボードを含む）は `mini_neo` として自動分類します。
- `!savelist`: 保存対象のワールド / スコアボード組を一覧表示します。
- `!saveremove <world> <scoreboard>`: 一致する組を削除します。ワールド名に空白が含まれても使えます。

登録するスコアボードの例:
- unlimited: `!savescore up`、`!savescore wingame`、`!savescore killCount`、`!savescore deathCount`
- MurderMystery: `!savescore Murder`、`!savescore Crew`、`!savescore wingame`
- MINIGAMER: `!savescore MINIGAMER_NEO` と `!savescore rocal`（Coin値が `MINIGAMER_NEO` 側にある場合は後者は不要です）。

## Discordコマンド

- `/player status <game> <name>`

`game` は `unlimited`、`MurderMysteryRe:MASTER`、`MINIGAMER` から選択します。`name` にはMinecraftのプレイヤー名を指定します。

既存の `/link`、`/delete`、Minecraft–Discordチャット連携、およびゲームBotの `/wordwolfex` と `/8192game` はそのままです。

## 同梱しないもの

Botトークンなどの `.env` は同梱していません。初回起動時に `Discord-GameBot\.env` を作成してください。

## /vs ステータス比較

`/vs name:<自身のMinecraft名> opponent:<相手のMinecraft名> world:<選択>`

ワールドは MINIGAMER / MurderMysteryRe:MASTER / unlimited から選べます。
既存の /player status と同じ保存データを使い、各項目を「自身 / 相手」と差で表示します。
未取得の項目は「未記録」、差は「比較不可」と表示します。同一人物の指定はできません。
Minecraft の対象スコアボードは事前に !savescore で登録してください。
接続中はデータ更新を試み、未接続時は保存済みデータで比較します。更新時刻も表示します。
変更を反映するには、起動中の main.py を Ctrl+C で終了し、再起動してください。
