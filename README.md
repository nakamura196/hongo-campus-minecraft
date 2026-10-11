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

Python 3.12、`pip install numpy laspy pyproj pillow`（scipy は使っていない）。

```
python fetch.py 09LD0863
python footprints.py data/09LD0863_bldg.geojson data/plateau/13105_53394650_bldg.gml data/plateau/13105_53394651_bldg.gml data/plateau/13106_53394651_bldg.gml --bbox -6800 -32100 -6400 -31800
python classify.py data/lidar/09LD0863.las data/09LD0863_bldg.geojson data/out
python build.py data/out/09LD0863_cells.npz out/hongo_test.txt --rows 100 164 --cols 190 254 --at 1000 -60 1000 --preview out/hongo_test.png
```

| スクリプト | すること |
|---|---|
| `fetch.py` | 点群 (.las) と PLATEAU の建物ファイルを `data/` に落とす（約 400 MB） |
| `footprints.py` | PLATEAU の屋根外形を EPSG:6677 の GeoJSON にする（09LD0863 で 237 棟） |
| `classify.py` | 点を 建物 / 木 / その他 / 地面 に分け、1 m 格子 `data/out/<tile>_cells.npz`（label, top, ground, origin）と確認用 PNG を出す。約 5 秒 |
| `build.py` | 格子の一部を切り出して Bedrock の `/fill` コマンド列にする |

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

## 状況 (2026-10-11)

- 64 × 64 m の試験範囲（図書館前広場、rows 100..164 / cols 190..254）のコマンドは作ったが、まだゲームに置いていない。
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
