# 旅人鯖 交流支援 Bot

## GitHub と Northflank への移行

1. この ZIP を展開し、展開した中身を GitHub の **Private** リポジトリの直下に登録します。登録後に `Dockerfile`、`.gitignore`、`.dockerignore`、`tabibito/` が見えることを確認してください。
2. Northflank の **Deploy repository** から **Combined** サービスを作成し、対象リポジトリとブランチを選択します。ビルド方式は **Dockerfile**、Dockerfile の場所は `/Dockerfile`、ビルドコンテキストはリポジトリのルートです。インスタンスは **1**、公開ポートは不要です。
3. 実行時の環境変数に `DISCORD_BOT_TOKEN` を登録します。実行時の **Secret file** として `/app/config.toml` を作り、元の `config.toml` の内容を設定します。サーバー・ロール・チャンネルの設定は元の値を引き継ぎ、`database` だけを例えば `"/data/tabibito-production.sqlite3"` に変更します。新規設定なら `config.example.toml` から作成し、ID の例を実際の値に置き換えてください。
4. サービスに **Single Read/Write** の永続ボリュームを追加し、コンテナ側のマウント先を `/data` にします。元の記録を引き継ぐ場合は、下記の手順で DB を移してから Bot を起動します。
5. 起動後は Northflank のログと Discord で接続を確認します。テスト運用では `dry_run = true` を使い、ロールの自動変更を有効にする際は元の運用に合わせて設定します。

### 既存の SQLite を引き継ぐ場合

元の Bot が参照している DB 名を、元の `config.toml` の `database` で確認します。元の Bot を停止し、SQLite のバックアップ機能で最新のスナップショットを作ります。元 ZIP に `-wal` がある場合は、元 DB と対応する `-wal` を同じフォルダに置いた状態で、バックアップを作成してください。DB 本体だけをコピーすると直近の記録を失う場合があります。次の例は元 DB が `data/tabibito-production.sqlite3` の場合です。出力先には未使用のファイル名を指定してください。

```bash
python -c "import sqlite3; from contextlib import closing; from pathlib import Path; source=Path('data/tabibito-production.sqlite3').resolve(); target=Path('tabibito-backup.sqlite3'); assert not target.exists(), 'Use a new backup filename'; exec('with closing(sqlite3.connect(source.as_uri() + \"?mode=ro\", uri=True)) as src, closing(sqlite3.connect(target)) as dst:\n    src.backup(dst)')"
```

バックアップを GitHub に置かず、安全な経路で Northflank の永続ボリュームに転送し、`database` に指定したファイル名で配置します。移行先の Bot が書き込みを始める前に配置し、同じトークンを使う旧 Bot は停止したままにしてください。新規運用の場合は起動時に空の DB が作成されます。

[Northflank のデプロイ手順](https://northflank.com/docs/v1/application/getting-started/build-and-deploy-your-code) · [永続ボリュームの設定](https://northflank.com/docs/v1/application/databases-and-persistence/add-a-volume)

Python 3.11 以上、discord.py、SQLite を使う単一サーバー向け Bot です。活動集計、卒業、交流支援といっぱしのロール判定を担当します。

## Discord 側の設定

1. 「交流支援」「いっぱし」「管理人」「モデレーター」「サポーター」を準備します。鯖主が別ロールの場合は、運営コマンドを許可する必要に応じて設定ファイルの `roles.operators` に追加します。
2. 「交流支援」カテゴリーを作り、「フリー雑談」「ファンタジーライフ雑談」「聞き専用テキスト」の3つのテキストチャンネルと「VC」を作成します。各チャンネルをカテゴリーの権限と同期します。
3. カテゴリーの `@everyone` に「チャンネルを見る」「接続」を拒否し、「交流支援」「管理人」「モデレーター」「サポーター」に許可します。テキスト送信などは運用に合わせて設定します。集計用に Bot のロールにも「チャンネルを見る」を許可します。Bot が VC に接続する権限は不要です。
4. 一般の「いっぱし」所持者が接続できないことと、4つの許可ロールの権限を Discord 側で確認します。サーバー所有者と `Administrator` はチャンネル制限を迂回できます。許可ロールを兼ねる「いっぱし」所持者には、兼任ロールによるアクセスが残ります。個別の上書きや他の許可ロールが付いている場合も権限に影響します。

Bot はカテゴリー、チャンネル、権限の作成・変更を行いません。

## Bot の登録と起動

1. Discord Developer Portal でアプリと Bot を作り、Bot ページの **Server Members Intent** を有効にします。コードで有効にする Intents は `guilds`、`members`、`voice_states`、`guild_messages` です。Message Content Intent と Presence Intent は不要です。
2. `bot` と `applications.commands` のスコープを使用し、対象サーバーにインストールします。Bot に「ロールの管理」と集計対象チャンネルの「チャンネルを見る」を付与します。Bot の最上位ロールを「交流支援」「いっぱし」より上に置き、両ロールを Discord 連携などが管理するロールにしないでください。Bot に Administrator や「チャンネルの管理」は不要です。
3. Discord の開発者モードを有効にし、サーバー・ロール・チャンネルの ID を取得します。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.toml config.toml
```

4. `config.toml` の ID を実際の値に置き換えます。トークンはシェルまたはサービス管理ツールから環境変数 `DISCORD_BOT_TOKEN` に設定します。Bash なら次のように非表示で入力できます。

```bash
read -rsp 'Discord Bot Token: ' DISCORD_BOT_TOKEN
export DISCORD_BOT_TOKEN
python -m tabibito --config config.toml
```

スラッシュコマンドは設定したサーバーにのみ登録されます。同じ Bot と SQLite に対して1プロセスだけを起動し、データベースは永続ディスクに保存してください。

## 初回導入

1. 初期値の `dry_run = true` で起動します。活動集計と記録修正は SQLite に保存され、予定するロール操作はログに表示されます。卒業コマンドは試行結果を返し、実際の卒業申請は作成しません。
2. 運営が既存の常連に「いっぱし」を手動付与します。テスト用サーバーかテストメンバーで、ロール順序、カテゴリー権限、投稿・VC 集計を確認します。
3. `dry_run = false` にして再起動します。初回データベース作成日時より前に参加したメンバーへの交流支援の自動付与は、`auto_enroll_existing = false` の間は抑止されます。この設定でも、卒業と二重ロールの解消は行います。
4. 既存メンバー全体にも自動付与を適用する場合は、`auto_enroll_existing = true` を最初にテストモードで確認できます。活動履歴が空の既存メンバーは、条件に該当します。確認後に有効化すると、Bot は自動処理を順次実行します。

SQLite を作り直すと初回基準日時も変わります。バックアップは Bot 停止後のデータベース一式か、SQLite のバックアップ機能を使って取得してください。

## 判定と集計

| 動作 | 条件 |
| --- | --- |
| 二重ロールの解消 | いっぱしと交流支援を所持しているとき、交流支援を解除 |
| 卒業 | 本人または運営の操作、VC 30回以上、VC 72時間以上のいずれか |
| 交流支援付与 | 自動付与の対象者が、新規参加、対象投稿0件、VC 5回未満、VC 24時間未満のいずれかを満たす場合 |

いっぱしを最優先に判定します。手動付与はメンバー更新イベントで検知します。卒業時はいっぱしの付与を先に確認するため、交流支援の解除に失敗した場合も再付与を防げます。自動処理はメンバーごとに直列化し、ロール変更直前に Discord API から現在の状態を取得します。Discord には手動操作と Bot 操作を一つのトランザクションにする機能がないため、両操作が重なったときは再取得と再判定で解消します。自動処理は1件ごとに間隔を置き、エラー時は60秒以降の定期判定で再試行します。Discord 自体のレート制限待機は discord.py が処理します。

- **テキスト**。サーバー全体で Bot が受信した人間の通常投稿と返信が対象です。自己紹介、Bot・Webhook、システム投稿は除外し、編集・削除で件数は変わりません。スレッドやフォーラム投稿は、Bot がイベントを受信できる範囲で集計し、親 ID でも包含・除外できます。Discord の履歴 API は閲覧可能なチャンネルで「メッセージ履歴を読む」権限が必要で、非公開スレッド、削除済み投稿などは完全に取得できません。この実装は履歴 API で遡及せず、導入後の受信イベントを計上します。
- **VC**。サーバーのボイスとステージを対象にします。対象 VC 間の移動は参加回数を増やしません。退出または対象外 VC への移動から300秒以内に戻ると同一参加になり、対象外にいた時間は加算されません。サーバーの AFK チャンネルは常に除外します。過去の VC 履歴は Discord から復元できません。
- **停止・再接続**。在室時間は約15秒ごとと退出時に確定します。再起動や接続断のあと、すでに対象 VC にいる人は観測再開時点から時間だけを計測し、参加回数は増やしません。未保存の末尾や接続断の検知前後の短い区間は誤差が出ることがあります。停止期間をまたぐ時間差は加算しません。
- **対象設定**。`text_include` と `voice_include` が空なら原則全チャンネルが対象です。除外 ID と自己紹介 ID が優先されます。設定変更は再起動後の新しい観測に適用されます。観測できなかった投稿や滞在は、必要に応じて管理者が記録を修正できます。

## コマンド

| コマンド | 使用者と動作 |
| --- | --- |
| `/交流支援終了` | 本人が卒業 |
| `/メンバーの交流支援終了 member` | 運営が指定メンバーを卒業 |
| `/活動記録 [member]` | 本人の記録を表示。運営は他のメンバーも照会可能 |
| `/記録修正 member [posts] [visits] [seconds]` | Administrator が指定した合計値を設定。省略した項目は維持 |
| `/ロール履歴 member` | 運営が直近10件のロール操作・記録修正を確認 |

運営は Administrator または `roles.operators` で指定したロールの所持者です。応答はコマンド実行者にのみ表示されます。手動変更は Bot が観測した内容を記録し、操作者 ID は Discord の更新イベントから取得できません。Bot の操作は実行結果も記録するため、同じ変更について観測記録と実行記録が残ることがあります。

## テスト

```bash
python -m unittest discover -s tests -v
```
