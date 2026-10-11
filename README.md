# hongo-campus-minecraft

東京大学本郷キャンパスを、航空レーザー測量の点群（LiDAR）と PLATEAU の建物データから
Minecraft（Bedrock 版）に再現する試み。
[ashtanmistal/minecraftUBC](https://github.com/ashtanmistal/minecraftUBC)（UBC キャンパスを LiDAR から作った例）に倣う。

## 使うデータ

- **点群**: G空間情報センター「東京都 区部点群データ 2024」(`tokyopc-23ku-2024`)。
  1/500 図郭 = 東西 400 m × 南北 300 m で 1 ファイル。座標は平面直角座標系 IX 系 (EPSG:6677)。
  - 図郭番号 `09LD08rc`: r = 北からの行 (300 m 刻み)、c = 西からの列 (400 m 刻み)。08 図葉の原点は x=-8000, y=-30000
  - キャンパス周辺: 09LD0852〜0854, 0862〜0864。いま扱っているのは **09LD0863**
    (x -6800..-6400, y -32100..-31800。三四郎池・本郷通り・図書館前広場)。
    安田講堂 (-6454, -31806) は北端にかかるので 0853 も要る
  - 中身 (09LD0863): 783 万点、約 65 点/m²、RGB 付き、最大 7 リターン。
    分類は「地面 (2)」と「それ以外 (1)」だけで、建物と木は自分で分ける必要がある
- **PLATEAU**: 建物 LOD1 (2025 年版、屋根外形 `lod0RoofEdge` と高さ付き)。3 次メッシュ単位。
  09LD0863 は 53394650 / 53394651 にまたがる（文京区 13105、台東区 13106）

## 手順

Python 3.12（3.9 でも動く）、`pip install numpy laspy pyproj pillow`（scipy は使っていない）。

```
python fetch.py 09LD0863
python footprints.py data/09LD0863_bldg.geojson data/plateau/13105_53394650_bldg.gml data/plateau/13105_53394651_bldg.gml data/plateau/13106_53394651_bldg.gml --bbox -6800 -32100 -6400 -31800
python classify.py data/lidar/09LD0863.las data/09LD0863_bldg.geojson data/out
python build.py data/out/09LD0863_cells.npz out/hongo_test.txt --rows 100 164 --cols 190 254 --at 1000 -60 1000 --preview out/hongo_test.png
python mcstructure.py data/out/09LD0863_cells.npz out/hongo_test.mcpack --rows 100 164 --cols 190 254 --at 1000 -60 1000
```

| スクリプト | すること |
|---|---|
| `fetch.py` | 点群 (.las) と PLATEAU の建物ファイルを `data/` に落とす（約 400 MB） |
| `footprints.py` | PLATEAU の屋根外形を EPSG:6677 の GeoJSON にする（09LD0863 で 237 棟） |
| `classify.py` | 点を 建物 / 木 / その他 / 地面 に分け、1 m 格子 `data/out/<tile>_cells.npz`（label, top, half, ground, origin）と確認用 PNG を出す。約 5 秒 |
| `build.py` | 格子の一部を切り出して Bedrock の `/fill` コマンド列にする |
| `mcstructure.py` | 同じ範囲を 64 × 64 マスごとの `.mcstructure` にし、ビヘイビアパック（`.mcpack`）にまとめる |
| `mesh.py` | 同じ範囲を三角形の面（glTF `.glb`）にする。VRChat（Unity）向けの試作。`web/viewer.html` でブラウザ確認。経過は `docs/notes/vrchat-world.md` |
| `points.py` | 同じ範囲の元の点群を、そのまま点の `.glb` にする（色つき、400 万点まで間引き）。`web/viewer.html?glb=../out/vr/points.glb` で見る |

### 分け方 (classify.py)

- PLATEAU の屋根外形の内側 = 建物
- 外側: 緑っぽい (ExG > 0.05) か、レーザーが突き抜けた (最後でないリターン) 点 = 木。それ以外 = その他（車・塀・墓石など）
- 周りの 5 マス以上が木なら「その他」も木にする
- 格子の結果: 建物 38%、木 42%、その他 3%、地面 16%、水 1%（三四郎池は点が返らないので「点なし = 水」）
- ラベル: 0 地面, 1 建物, 2 木, 3 その他, 4 水, 5 低い物（地面から 0.5 m 未満。縁石など）
- 弱いところ: 図書館の東、木の下の細い建物が縞になる。PLATEAU に無い構造物は「その他」になる（未確認）

### ブロックへの置き換え (build.py)

1 マス = 1 ブロック、高さも 1 m = 1 ブロック。行 0 が北端で、Minecraft の z は南向き、x は東向き。
`--at X Y Z` は切り出した範囲の北西の角の位置で、Y はその範囲でいちばん低い地面の高さ。

- 地面: 土の上に草ブロック。低い物 = 石。水 = 水
- 建物: 屋根の高さまで薄灰色コンクリートで埋める
- 木: 葉だけ、上から 4 段（`persistent_bit` 付きで消えない）
- その他: 丸石、最大 3 段
- コマンド数を減らすため、土は 1 段ずつの層で、それ以外は同じ部材のマス同士で長方形にまとめる。
  64 × 64 m の試験範囲で 1,965 本

### ストラクチャーで置く (mcstructure.py)

`build.py` と同じブロックの積み方（`columns()`）を使い、コマンドではなくブロックの塊のファイルにする。
空いている所は空気で埋めるので、元からあったブロックは消える。
Windows 側での使い方:

1. `.mcpack` をダブルクリックして Minecraft に取り込む
2. ワールドの設定で、このビヘイビアパックを有効にする（実績は無効になる）
3. 置く場所の近くに移動してから（遠いチャンクには置けない）`/function hongo/load`

試験範囲は 1 ファイル（約 1.1 MB）。読み戻して `/fill` 1,965 本の結果とブロック単位で照合し、
14 万 3,360 マス中の不一致は 0（2026-10-10）。ゲームでの読み込みはまだ試していない。
ブロックの状態のバージョンは 1.21.0 として書いている（新しいゲームは読むときに変換する）。

### Mac の Java 版で試す (server.py)

Java 版なら Mac で確かめられる。公式のサーバーを手元で動かし、命令は RCON（外から命令を送る仕組み）で送る。
サーバーは 127.0.0.1 でだけ待ち受ける。中身は `server/`（Git に入れない）。

```
python build.py data/out/09LD0863_cells.npz out/hongo_java.zip --rows 100 164 --cols 190 254 --at 0 -60 0 --java
python server.py setup out/hongo_java.zip
python server.py start                       # 別のターミナルか、裏で動かす
python server.py cmd "forceload add 0 0 63 63" "function hongo:load"
```

- プレイヤーがいない場所は読み込まれないので、置く前に `forceload` が要る
- ゲームからは「マルチプレイ → ダイレクト接続 → `localhost`」
- ブラウザで 3D の地図を見る: `python server.py map`（別のターミナルで。サーバーと同時に動かしてよい）。
  BlueMap 5.28 の CLI を `server/bluemap/` に入れ（ハッシュを照合）、`http://127.0.0.1:8100` で地図を出す。
  ゲームを開かなくてよく、メニューで隠れることもない。ワールドが変わると 1 分ほどで地図も描き直す。
  安田講堂の付近: `http://127.0.0.1:8100/#overworld:150:-35:95:90:1.2:1.0:0:0:perspective`
  （初回に Mojang からクライアントの jar を取ってきて、ブロックの形と模様に使う。地図の外側の黒い帯は、まだ描いていない場所）
  置き直したあとは、1 分ほど待ってからブラウザを再読み込みする
- 画面を撮る: `python server.py shot out.png`。ゲームのウィンドウが VS Code などの後ろに隠れていても撮れる
  （ウィンドウ番号を Swift で引き、`screencapture -l` に渡す）。Claude が移動（`tp`）と撮影を繰り返して見た目を点検するのに使う
- **フォーカスが外れたときの一時停止を切る**: 既定では、ほかのウィンドウに移るとゲームが一時停止メニューを出し、
  撮った画面がメニューで隠れる。ゲームの画面で **F3 を押したまま P** を押すと切り替わる（チャット欄に 1 行出る）
  - **Mac では F3 だけを押すと Mission Control（ウィンドウが並ぶ画面）が開く。** fn キー
    （キーボード左下の端、「fn」か地球儀 🌐 の刻印）を足し、**fn と F3 を押したまま、離さずに P** を押す
  - F3 を離してから P を押すと、別々に効く。左上に文字の多い情報表示（F3）と「社交設定」の画面（P）が開くので、
    「完了」で閉じ、fn+F3 で情報表示を消してからやり直す
  - fn キーの無い外付けキーボードでは、システム設定 → キーボード →「F1、F2 などのキーを標準のファンクションキーとして使用」
    をオンにすると、fn なしの F3+P で効く
  - それでも効かなければ、ゲームを終了してから `~/Library/Application Support/minecraft/options.txt` の
    `pauseOnLostFocus` を `false` にする（ゲームの起動中に書き換えても、終了時に上書きされる）
- 2026-10-10: 試験範囲を置き、ランダムな 200 か所を照合して 199 か所一致。
  残る 1 か所は、上に丸石が載った草ブロックをゲームが土に変えたもの（ゲームの仕様）

### 安田講堂と三四郎池 (2026-10-10)

図書館前広場だけでは場所が分かりにくいので、安田講堂と三四郎池を含む範囲に移した。
安田講堂は 09LD0853/0854 の境にかかるので、4 図郭をつないで使う。

```
for t in 09LD0853 09LD0854 09LD0864; do python fetch.py $t; done
python footprints.py data/hongo_bldg.geojson data/plateau/*.gml --bbox -6800 -32100 -6000 -31500
python classify.py data/lidar/09LD0853.las data/lidar/09LD0854.las data/lidar/09LD0863.las data/lidar/09LD0864.las data/hongo_bldg.geojson data/out
python build.py "data/out/09LD0853+09LD0854+09LD0863+09LD0864_cells.npz" out/yasuda.zip --rows 200 488 --cols 220 476 --at 0 -60 0 --java --preview out/yasuda.png
```

- 4 図郭で 2,880 万点、800 × 600 m、建物 854 棟。範囲は 256 × 288 m、コマンド 33,180 本
- **色**: 点の RGB（航空写真の色）を使う。`classify.py` が 1 m ごとの色と、属する PLATEAU の建物番号を保存する
  - 建物は 1 棟 1 色。屋根の色の中央値で決める。写真の色は淡いので、灰色か色付きかを先に分ける。
    茶色系ははっきり色があるときだけ色付き、緑がかった灰色は銅板屋根とみなす。
    安田講堂 (139,124,113) → mud_bricks、法文棟の屋根 (166,170,161) → weathered_copper
  - 地面は 5 × 5 m の色の中央値から 草 / 石 / 砂利 / 土の道 などを選ぶ。茶色の点々がまだ残る
  - Java では銅は錆びが進むので waxed_ 付きを使う
- `--plain` で以前の無彩色に戻せる
- **建物の整形** (`roofs.py`、既定で有効、`--raw` で無効): 壁を PLATEAU の輪郭にそろえ、屋根を平面に当てはめて
  アンテナや点のばらつきによる 1 ブロックの凸凹を消す。段差は 38% 減り、元の高さとの差は中央値 0.1 m。
  コマンドは 31,482 本。詳しくは `docs/notes/voxel-reconstruction.md`
  そのとき、屋根の上の塔の縁が太らないよう、縁の格子は「格子の半分を覆う高さ」（`classify.py` の `half`）に下げる
  （安田講堂の時計台は 128 → 107 m²）。`half` は classify.py を走らせ直すと npz に入る
- **木の整形** (`trees.py`、既定で有効、`--raw` で無効): 建物に接する葉を除き（壁と樹冠の間に 1 ブロックの隙間）、
  樹冠を 1 本ずつに分けて、それぞれに幹を立てる。安田講堂の範囲で壁沿いの葉 1,806 格子を除き、木は 284 本
- 置き直すときは、先に範囲を空気で消す（古い凸凹が残るため）。16 × 16 の柱ごとに `fill x -60 z x+15 30 z+15 air`

### 本郷地区全体 (2026-10-10)

本郷・弥生・浅野キャンパスを含む 800 × 1,500 m。LiDAR 10 区画をつなぐ。

```
for t in 09LD0833 09LD0834 09LD0843 09LD0844 09LD0853 09LD0854 09LD0863 09LD0864 09LD0873 09LD0874; do python fetch.py $t; done
python footprints.py data/campus_bldg.geojson data/plateau/*.gml --bbox -6800 -32400 -6000 -30900
python classify.py data/lidar/09LD08{3,4,5,6,7}{3,4}.las data/campus_bldg.geojson data/out
python build.py "data/out/09LD0833+09LD0834+09LD0843+09LD0844+09LD0853+09LD0854+09LD0863+09LD0864+09LD0873+09LD0874_cells.npz" out/campus.zip --rows 0 1500 --cols 0 800 --at 2048 -60 0 --java --name campus --piece 256
```

- 置くときは区画ごとに `forceload add` → `function hongo:campus_<i>` → `forceload remove`（24 区画）。
  ワールドの x 2048..2847 z 0..1499、安田講堂前は `/tp @s 2378 -37 890`
- classify.py は最大 14 GB ほどメモリを使う

### 本郷地区＋周辺の通り (2026-10-10)

1,100 × 1,650 m。LiDAR 24 区画（09LD08{3..8}{2..5}）を `--bbox` で切る。
Java 版では屋根と地面にハーフブロック・階段が入る（smooth.py、`--blocky` で無効）。
草地と木の下の地面は、歪んだ木の半ブロック・階段を置き、リソースパックで草の見た目に描き替える
（`python grasspack.py out/grass_pack.zip`、`--no-grass` で無効）。パックが無いと青緑の木の板に見える。

```
python footprints.py data/wide_bldg.geojson data/plateau/*.gml --bbox -6900 -32550 -5800 -30900
python classify.py data/lidar/09LD08{3,4,5,6,7,8}{2,3,4,5}.las data/wide_bldg.geojson data/out --bbox -6900 -32550 -5800 -30900 --name wide
python build.py data/out/wide_cells.npz out/wide.zip --rows 0 1650 --cols 0 1100 --at 3072 -60 0 --java --name wide --piece 256
```

- ワールドの x 3072..4171 z 0..1649（35 区画、`hongo:wide_<i>`）。安田講堂前は `/tp @s 3502 -37 890`
- classify.py は最大 18 GB、build.py は 11 分ほどかかる

## 状況 (2026-10-11)

- 64 × 64 m の試験範囲（図書館前広場、rows 100..164 / cols 190..254）のコマンドと `.mcpack` は作ったが、まだゲームに置いていない。
  置き場所は未定（本郷用に新しいスーパーフラットのワールドを作る案）
- 広場の舗装に石と草がまだらに出る（低い物の判定が点々と出るため）
- **Mac では Bedrock 版が動かない**（Mac 版は Java 版のみ）。ゲームへの書き込みは Windows 側で行う。
  Windows 側の WebSocket 送信の仕組み (`mcws.js`) は別リポジトリ nakamura88/minecraft にある

## 手法の見直し (2026-10-11 調査)

- minecraftUBC (最終更新 2025-01, Java 版のみ) の考え方 = 1 m 格子の柱を色の近いブロックで埋める、は今も有効。
  あちらは建物と木を PointNet++ (学習した分類器) で分けるが、こちらは PLATEAU の輪郭と色で分けていて軽い
- 改善の候補（優先順）:
  1. **書き込み方**: コマンド送信 (約 40 本/秒) は小さな範囲向け。広げる前に `.mcstructure` を区画ごとに作って
     `/structure load` で読む方式か、[Amulet-Core](https://github.com/Amulet-Team/Amulet-Core) でワールドを直接書く方式に切り替える
  2. 木を 1 本ずつ分け、幹と樹冠にする（minecraftUBC と同じ）
  3. PLATEAU LOD2 がある所は屋根の形を使う。色は点の RGB から近いブロックを選ぶ
  4. [Arnis](https://github.com/louis-e/arnis)（OpenStreetMap から生成、Bedrock 出力あり）で同じ範囲を作り、道路・水の見た目を比べる
- PLATEAU を Minecraft に変える既製の道具は見つからなかった

`data/` と `out/` は Git に入れない（`fetch.py` で取り直せる）。

## 配布用のワールド（本線だけ）

```
python dist.py out/wide.zip out/grass_pack.zip out/hongo-campus.zip
```

- `server-dist/`（Git 外）で 2 台目のサーバ（ポート 25566、RCON 25576）を立て、空の超平坦ワールドに本線（wide）だけを置く。
  ふだんのサーバ（`server/`）は止めなくてよい
- 出発地点は安田講堂前（3502 −37 890、東向き）。昼・晴れに固定し、敵は出ない
- 置き終えたらデータパック（置くための命令だけ、50 MB）を外し、草のパックを `resources.zip`、出典を `README.txt` として同梱する
- 使う人は zip を `.minecraft/saves/` に展開する。開くと同梱のパックを使うか聞かれる
- 作り直すときは `server-dist/hongo-campus` を消してから実行する
- 公開: https://github.com/nakamura196/hongo-campus-minecraft-world （Releases に zip、ワールドは CC BY 4.0）

## ライセンス

コードは MIT（`LICENSE`）。作ったワールドは CC BY 4.0（配布リポジトリの README に出典の書き方）。
