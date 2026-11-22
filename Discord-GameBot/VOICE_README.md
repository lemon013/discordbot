# VCチャット読み上げ

音楽・サウンドボード・読み上げは1つのVC接続で混ぜて再生します。現在はVOICEVOX専用です。Googleの認証・課金登録は不要です。VOICEVOXの実行環境は別途必要です。Discordユーザーの声を録音する機能ではありません。

## 初回の準備

更新後のrequirements.txtでパッケージを再インストールしてください。同時再生はPCMをOpusへ変換するため、FFmpegに加えネイティブのOpusライブラリが必要です。Windowsではdiscord.py同梱ライブラリを使用します。Linuxで見つからない場合はホスト管理者にlibopus（Debian系ではlibopus0）の導入を依頼してください。既存ライブラリが標準の探索先にない場合、`VOICE_OPUS_LIBRARY` にライブラリの絶対パスを設定できます。DAVE鍵交換は既存の互換VoiceClientを継続使用します。

### MWS無料プラン：WEB版VOICEVOX APIを使う

PCやVOICEVOXエンジンの常時起動は不要です。`VOICEVOX_URL` が未設定の場合、非公式のWEB版VOICEVOX API（tts.quest）を使って文章を音声に変換します。Googleの課金登録は不要です。音声エンジンやモデルをMWSにインストールしません。

1. GitHubの最新コードをMWSへ反映し、Botを再起動します。
2. MWSの `VOICEVOX_URL` は未設定のままにします。以前localhostなどを入れた場合は解除してください。
3. DiscordでVCへ参加し、そのVCのチャットで `/voice join` を実行します。
4. 「こんにちは」と投稿して読み上げを試します。
5. `/voice change` のvoice欄で声、speed欄で0.5〜2.0倍速を選びます。

APIキーなしで使用します。任意で取得したキーがある場合のみ `TTS_QUEST_API_KEY` に設定できます。キーの自動発行や制限回避は行いません。

利用者が選べる声は、規約を確認した「ずんだもん」「四国めたん」「九州そら」「中国うさぎ」です。各スタイルを含め21候補を用意しています。API側が選んだ声を提供していない場合は案内し、別の声へ勝手に切り替えません。Google音声は使用しません。

Botのプロフィールに次を掲載してください。

```text
音声：VOICEVOX:ずんだもん、VOICEVOX:四国めたん、VOICEVOX:九州そら、VOICEVOX:中国うさぎ
音声生成：非公式WEB版VOICEVOX API（tts.quest）
音源利用規約：https://zunko.jp/con_ongen_kiyaku.html
```

読み上げ開始時には、チャット本文を外部APIへ送信することをVCチャットに通知します。`/voice change` 等には「クレジットはBotのプロフィール欄を参照してください。」と表示します。音源の禁止用途を含む規約も守ってください。生成した音声のURLは利用者へ公開しません。公開サービス側でのデータの取り扱いは同サービスの案内を確認してください。

APIには混雑・利用上限があります。APIが返すretryAfterを尊重し、連打で再試行しません。1つのBot内で合成要求を直列化し、音声完成を3秒間隔で確認します。生成待ちは最大90秒です。混雑時は通常のチャットより大きな遅延が出る可能性があります。速度変更は取得した音声をローカルFFmpegのatempoで処理し、声の高さを保ちながら変更します。

外部API資料：
- https://voicevox.su-shiki.com/su-shikiapis/ttsquest/
- https://github.com/ts-klassen/ttsQuestV3Voicevox
- https://zunko.jp/con_ongen_kiyaku.html

### 自分のVOICEVOXエンジンを使う場合

従来どおり `VOICEVOX_URL` にBotから到達できるエンジンの接続先を設定すると、そのエンジンを使います。例えば同じマシンなら `http://127.0.0.1:50021` です。クラウドのlocalhostは自宅PCを指しません。MWS無料プラン内でのエンジン同時起動は、実行メモリの問題があるため追加していません。

`/voice diagnose` は管理者がコンテナ・Botの使用メモリを調べるコマンドです。ファイルシステムの空き容量は、MWSアカウントの保存容量枠の残量とは別です。APIキーや接続先URLの値は出しません。

## コマンド

| コマンド | 動作 |
| --- | --- |
| `/voice diagnose` | MWS実行環境・使用メモリを表示（管理者用） |
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
