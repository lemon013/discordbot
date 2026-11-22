# VCチャット読み上げ

音楽・サウンドボード・読み上げは1つのVC接続で混ぜて再生します。Google Cloud Text-to-SpeechとVOICEVOXの2種類に対応します。設定しないサービスは利用できません。Discordユーザーの声を録音する機能ではありません。

## 初回の準備

更新後のrequirements.txtでパッケージを再インストールしてください。同時再生はPCMをOpusへ変換するため、FFmpegに加えネイティブのOpusライブラリが必要です。Windowsではdiscord.py同梱ライブラリを使用します。Linuxで見つからない場合はホスト管理者にlibopus（Debian系ではlibopus0）の導入を依頼してください。既存ライブラリが標準の探索先にない場合、`VOICE_OPUS_LIBRARY` にライブラリの絶対パスを設定できます。DAVE鍵交換は既存の互換VoiceClientを継続使用します。

### VOICEVOX

別途VOICEVOX ENGINEを起動し、Botから到達できる接続先を環境変数 `VOICEVOX_URL` に指定します。同じマシンなら例は `http://127.0.0.1:50021` です。クラウドBotでlocalhostを指定した場合はクラウド自身を指します。PCで起動したエンジンが自動的につながるわけではありません。MWSから使う場合は、別ホストのエンジンなど、MWSからアクセスできる接続先を準備してください。無認証エンジンをそのままインターネットへ公開しないでください。

選択対象は利用規約を確認した「ずんだもん」「四国めたん」と、それぞれのエンジンが提供するスタイルです。声の候補は `/speakers` から取得します。その他のキャラクターは自動公開しません。

Botのプロフィールへ、少なくとも次を掲載してください（プロフィールの変更は管理者が手動で行います）。

```text
音声：VOICEVOX:ずんだもん、VOICEVOX:四国めたん
音源利用規約：https://zunko.jp/con_ongen_kiyaku.html
```

`/voice change` 等にも「クレジットはBotのプロフィール欄を参照してください。」と表示します。クレジット表示に加え、音源規約の禁止用途も遵守してください。録音・配信等で音声を再利用する場合も、それぞれの利用条件を確認してください。

### Google Cloud Text-to-Speech

Google Cloud側でプロジェクトのCloud Text-to-Speech APIと必要な課金設定を有効にします。Googleの利用料金・割当が適用されます。Geminiのキーをそのまま使う設定ではありません。

認証は次のどちらかです。

- 標準のApplication Default Credentials。サービスアカウントを使う場合は、認証JSONファイルへの絶対パスを `GOOGLE_APPLICATION_CREDENTIALS` に設定します。ファイルはリポジトリへ入れず、ホストの秘密情報用の保存先へ配置します。
- 対象APIで利用できるGoogle CloudのAPIキーを `GOOGLE_TTS_API_KEY` に設定する方法。キーをコードやDiscordへ投稿しないでください。

日本語Standard/WaveNet音声をAPIから取得し、声と0.5〜2.0倍速を選べます。VOICEVOX_URL設定時の初期音声はずんだもん（ID 3）、それ以外はGoogleのja-JP-Standard-Aです。各利用者が `/voice change` で選び直せます。Google利用時は読み上げ対象の文章をGoogleへ送信します。VOICEVOX利用時は指定エンジンへ送信します。

公式資料：
- https://docs.cloud.google.com/text-to-speech/docs/reference/rest/v1/text/synthesize
- https://voicevox.github.io/voicevox_engine/api/
- https://voicevox.hiroshiba.jp/term/
- https://zunko.jp/con_ongen_kiyaku.html

## コマンド

| コマンド | 動作 |
| --- | --- |
| `/voice join` | 自分が参加しているVCへ接続し、そのVC内チャットの読み上げ開始・再開 |
| `/voice stop` | 読み上げと待機文だけ停止。音楽・サウンドボード・VC接続は継続 |
| `/voice leave` | 読み上げ・音楽・サウンドボードを停止して退出 |
| `/voice change provider: voice: speed:` | 自分の声と速度を変更・保存。voice欄は候補から選択 |
| `/voice list` | 参加中VCのメンバーの声・速度を表示（最大50人、表示1900文字まで） |
| `/voice skip` | 現在の読み上げ・合成をスキップ。音楽は継続 |
| `/voice clear` | 待機文だけ全消去。現在の読み上げは継続 |
| `/voice volume music: reading: board:` | それぞれの音量を0〜100%で変更。変更はサーバー管理権限が必要 |
| `/voice duck enabled: volume:` | 読み上げ中に音楽を下げる設定。volumeは通常の音楽音量に対する割合。管理者用 |
| `/voice settings` | 読み上げ対象の設定を表示。変更は管理者用 |
| `/voice dictionary add word: reading:` | 読み方の登録（管理者用） |
| `/voice dictionary remove word:` | 読み方の削除（管理者用） |
| `/voice dictionary list` | 辞書一覧 |

`/voice settings` では `names`（名前を読む）、`urls`（省略・リンクと読む・そのまま）、`emoji`、`attachments`、`bots`、`max_chars`（10〜300文字）を変更できます。省略したオプションは維持します。

初期値：音楽70%・読み上げ100%・サウンドボード80%、読み上げ中の音楽は通常の30%。名前・絵文字・Bot投稿は読まず、URLは「リンク」、添付は「添付ファイル」、長文は150文字で切ります。コードブロックとスポイラーの本文は省略します。コマンドらしい投稿（`/`・`./`・`!`から始まる投稿）、Bot自身の投稿、VCに参加していない人の投稿は読みません。Bot投稿をONにしても対象VCに参加しているBotだけです。

文章は送信順で、合成終了と再生終了を待って次へ進みます。待機は30件まで、60秒以上古い待機文は省略します。サウンドボードは同時3件まで。音声を重ねた結果が大きすぎる場合は振幅を制限します。音楽の一時停止・スキップは音楽のレーンだけを操作します。

声と速度はユーザーごと、音量・読み上げ対象・辞書はサーバーごとに `levels.sqlite3` の専用テーブルへ保存し、既存のDiscordデータベースバックアップに含めます。生のチャットや生成音声は保存データベースに記録しません。待機文と読み上げ有効状態は再起動時にリセットします。

読み上げがONの間は無音でもVCに残ります。`/voice stop` 後は、音楽・音声・キューがすべてなくなって60秒で退出します。`/sound leave` も全音声と読み上げを終了します。`/sound stop` は音楽・添付ファイルの再生と音楽キューを停止しますが、読み上げ・サウンドボードは継続します。

Message Content IntentがDiscord Developer PortalとBotの設定で有効である必要があります。APIキー・サービスアカウントの秘密鍵・プロバイダーの生のエラー応答をログに出す処理は追加していません。
