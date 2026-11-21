# VC内チャットから音声ファイルを再生

1. 音声を流したいVCに参加します。
2. そのVCのチャットを開き、`/sound play` の `file` 欄に音声ファイルを添付して実行します。
3. Botが同じVCに参加して再生します。

`/sound stop` で停止、`/sound leave` で退出します。同じVCに参加しているユーザーが操作できます。通常のテキストチャンネルやステージチャンネルには対応しません。

音が聞こえない場合は `/sound test` で3秒の440Hzテスト音を流せます。元ファイルに依存しない音源で切り分けるためのコマンドです。続けて `/sound status` を実行すると、現在または直近の読込フレーム数・バイト数・暗号化準備状態・ライブラリのバージョンを本人だけに表示します。これらの値にキーやトークンは含めません。

- 対応：MP3、WAV、OGG、OPUS、M4A、AAC、FLAC、WEBM、MP4。MP4は映像を流さず、含まれる音声だけを再生します。音声が含まれない動画は案内して終了します。最大25MBです。Discord側の添付上限が小さい場合はそちらが優先されます。
- 1サーバーで同時に1件再生します。再生中に別のファイルを流す場合は、先に停止してください。キュー・音声ファイルの登録機能はありません。
- 再生終了・停止後、60秒間再生されなければ自動退出します。
- 実行直後に受付メッセージを表示し、ファイル取得→音声確認→VC接続と進捗を更新します。処理中に前回の自動退出タイマーが動かないようにします。同じサーバーで準備・操作中の場合、次のコマンドは待機させず案内します。
- VC接続は20秒を目安に打ち切り、接続失敗の終了処理を待たずにタイムアウトを案内します。終了処理が完了するまでは再接続を受け付けません。通信やイベントループ自体が停滞している場合は、表示の更新が遅れることがあります。
- 添付ファイルを一時保存して音声を確認し、再生終了・停止・エラー時に削除します。Botが強制終了した場合はOSの一時フォルダーに残る場合があります。
- Botには対象VCの「チャンネルを見る」「接続」「発言」権限が必要です。

## 起動環境

両方のrequirements.txtに音声用依存関係を追加しています。通常の起動・MWSのGitHub更新時に依存関係を再インストールしてください。手動なら `python -m pip install -r requirements.txt` の後でBotを再起動します。

`discord.py[voice]==2.7.1` と `davey==0.2.0` に固定し、`voice_compat.py` で0.2系の鍵遷移手順へ対応しています。`discord.py[voice]` はPyNaClも導入します。`imageio-ffmpeg` の同梱FFmpegで音声をOpusへ変換します。FFmpegを同梱していないプラットフォームでは、FFmpegを別途導入し、`IMAGEIO_FFMPEG_EXE` に実行ファイルの絶対パスを指定してください。

`davey 0.2.0` では `process_commit` / `process_welcome` 後に `prepare_transition` と `execute_transition` で暗号鍵を有効にする方式へ変更されました。discord.py 2.7.1の標準処理はこの変更に対応しておらず、VC接続が成功してもDAVE準備が完了しません。互換VoiceClientをこの機能にだけ使用し、サーバーのPrepare/Executeイベントに従って鍵を切り替えます。遷移IDが0の場合はprepare時に即適用されるため、executeを重ねて呼びません。無効なCommit/Welcomeは鍵を有効化せず、SDKの再生成処理に渡します。鍵や受信ペイロードはログに出しません。暗号化を無効化する回避策やSDK全体へのモンキーパッチは使用しません。公式の変更点：https://github.com/Snazzah/davey/releases/tag/py-0.2.0

`/sound status` の「DAVE互換処理：0.2系の鍵切り替え対応版」と `discord.py=2.7.1、davey=0.2.0` を確認してください。`voice_compat.py` も更新に含めます。依存パッケージが異なる場合はMWSでパッケージを再インストールするか、起動ルートで `python -m pip install -r requirements.txt` を実行してから再起動します。ログの `DAVE key transition prepared` / `DAVE key transition activated` は遷移ID・バージョン・可否だけを示し、暗号鍵を含みません。

MWS側でDiscordの音声接続（UDPなど）が許可されている必要があります。音声対応のネットワーク設定がある場合はそちらを選んでください。Botはホスティング側の通信制限を変更できません。

ログの「Voice handshake complete. Endpoint found」は音声サーバーの接続先が見つかった段階です。その後の「Voice connection complete.」より前に「Timed out connecting to voice」が出る場合、音声接続は成立していません。再生後の自動退出ではなく、接続失敗後に退出しています。接続先の取得が成功しただけでは、UDPを含む音声通信が通ることは確認できません。

コマンド結果は実行者だけに表示されますが、音声はVCの参加者に聞こえます。添付音声はFFmpegで処理し、Gemini APIには送信しません。

`discord.player: Write error ... AttributeError: 'int' object has no attribute 'write'` はFFmpegのエラー出力先の指定に起因する不具合でした。再生用FFmpegには書き込み可能なファイルオブジェクトを渡し、再生終了時に閉じるよう修正しています。Botの緑の発話表示だけでは音声データが参加者まで正常に届いていることは確認できません。

Opusは20ms単位に固定して変換し、OggのOpusHead・OpusTagsは音声フレームとして送信しません。SDKがDAVE暗号化を必要としている場合は、暗号化準備の完了を最大10秒待ちます。準備が完了しなければ再生を開始せず案内します。音源側のエラーはSDKの再生コールバックへ引き継ぎます。

`Sound audio frames started` と `Sound audio completed` のログで、音声の読込開始と終了時のフレーム数を記録します。読込が増えていても参加者への到達は保証できないため、テスト音も聞こえない場合は `/sound status` の結果と当該ログを用いて送信・暗号化・ホスティング経路を確認してください。
