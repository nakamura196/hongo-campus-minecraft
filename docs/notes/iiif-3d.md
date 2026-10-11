# IIIF 3D で 3d.ldas.jp に載せる

out/vr の glb を IIIF の manifest にして 3d.ldas.jp（nakamura196/iiif-3d-viewer）で見せ、
建物に説明の注釈を付ける試み。

## 2026-10-10 調査: 公式仕様とビューアの対応

### IIIF Presentation 4.0（3D を含む版）

- まだ草案。https://iiif.io/api/presentation/4.0/ は「4.0.0-draft」、最新の安定版は 3.0.0。
  GitHub IIIF/api のマイルストン「Presentation 4.0」は未完了 94 件・期限なし（2026-10-10 時点）
- 置き方: `Scene` の中に `painting` の Annotation、body は `type: "Model"`、`format: "model/gltf-binary"`。
  公式例 https://iiif.io/api/presentation/4.0/example/uc06_3d.json
- 点への注釈: `commenting` ＋ TextualBody。位置は `PointSelector {x,y,z}`。
  公式例 https://iiif.io/api/presentation/4.0/example/uc08_3d_annotation.json
  （公式例は位置を body の `position` に入れ、target は Scene 全体。3d.ldas.jp は target の selector を読む。形が違う点に注意）
- 注釈とカメラは `scope` で結ぶ。カメラに `behavior: ["hidden"]` を付ければ、注釈を選んだときだけ使われる
- 座標: 右手系・+y 上。単位は仕様に明記なし（Issue #2401 が open）。`spatialScale` で実寸に対応づける
- 領域（3D の範囲）の標準の書き方は未確定（WktSelector の議論 Issue #2367）
- **段階的な読み込み・3D Tiles・点群・LOD は仕様に記載なし**。IIIF/api・IIIF/3d の Issue にも
  "3D Tiles" "point cloud" "lidar" は 0 件。近いのは IIIF/3d #31（大きなテクスチャ、議論なし）。
  仕様で使えるのは `Choice`（複数の候補から 1 つ。例は動画の低解像度・高解像度。先頭が既定）だけ
- 4.0 の 3D に対応したビューアは、IIIF の対応表（cookbook の matrix-v4）で Voyager（Smithsonian）と Kompakkt のみ

### 3d.ldas.jp（iiif-3d-viewer、origin/main 6c0860a 時点）

- 呼び方: `https://3d.ldas.jp/ja/viewer?manifest=<URL を encode>`
- 4.0 草案の形をそのまま読む（3.0 の独自拡張は読み込み時に 4.0 へ変換）。読むのは最初の Scene だけ
- 注釈は `Scene.annotations` から。target の PointSelector か WktSelector（POLYGON Z、重心を位置にする）。
  見出しは `label`、本文は HTML 可、`purpose: tagging` はタグ、`type: Image` は写真
- モデルの body に `Choice` を書けば、利用者が手で切り替えられる（自動ではない）
- **段階的な読み込みは無い**。drei の `useGLTF` に任せている。Draco・Meshopt の圧縮は drei の既定で解ける見込み（未確認）
- glb と manifest は https で、3d.ldas.jp からの CORS を許す場所に置く必要がある
- 前例: powell-street（branch `iiif3d`、`scripts/export_iiif3d.mjs`）。置き場所は未定のまま
  （Cloudflare Pages は Access で本人限定のため 3d.ldas.jp から読めない）

### 手元の材料

- `out/vr/hongo.glb` 9.6 MB、安田講堂周辺の約 260 × 295 m。箱のメッシュ（本人評価は低い）
- `out/vr/points.glb` 80 MB、400 万点（glTF の POINTS。points.py の --max で上限 400 万）。見た目は良いが、圧縮なしでは重い
- 建物名: PLATEAU 854 棟のうち name があるのは 7 件で、建物名と言えるのは「本郷郵便局」程度。別の出典が要る

## 2026-10-10 方針（本人と決定）

- 載せるのは点群（points.glb）を軽くしたもの。箱のメッシュは使わない
- 段階的な読み込み: manifest は仕様どおり Choice で軽い順に並べ、`behavior: ["progressive"]` を付ける
  （仕様に無い値。知らないビューアは無視して先頭＝軽い版を出すので壊れない）。
  3d.ldas.jp を直し、先頭を出してから末尾を裏で読み、届いたら差し替える
- 3D Tiles は採らない（IIIF の枠の外になる）
- 公開はまだ。手元で確かめる

## 2026-10-10 点群を軽くする（lod.py ＋ gltfpack 1.2.0）

| 版 | 点数 | 大きさ | 作り方 |
|---|---|---|---|
| 元 points.glb | 400 万 | 80.0 MB | 位置 float32、色 uint16 RGBA（20 バイト/点） |
| 位置を 1 cm の整数・色 8 ビット | 400 万 | 48.0 MB | lod.py（KHR_mesh_quantization） |
| 同 ＋ Meshopt（1 cm のまま `-noq`） | 400 万 | 37.7 MB | 2 割しか縮まない |
| 高精細 hongo-full.glb | 400 万 | 19.0 MB | gltfpack `-c`（位置 14 ビット＝約 1.8 cm） |
| 中 hongo-mid.glb | 117 万 | 6.4 MB | 0.5 m の立方体ごとに 1 点 → `-c` |
| 軽量 hongo-light.glb | 40 万 | 2.3 MB | 1 m の立方体ごとに 1 点 → `-c` |

- 位置 13 ビット（約 3.5 cm）で 16.9 MB、12 ビット（約 7 cm）で 15.2 MB。縮み方が小さいので 14 ビットにした
- 点を Morton 順（近い点が並ぶ順）に並べ替えても大きさは 1 バイトも変わらなかった。gltfpack が圧縮前に自分で並べ替えている
- 圧縮後の glb には材質が無く、three.js は点を 1 画素で描く。ビューア側で、点の間隔（上から見た面積 ÷ 点数）の 1.5 倍の大きさにした
- gltfpack（npm、作者 zeux＝meshoptimizer の作者、依存なし）は scratchpad に入れた。wasm 版は作業フォルダの外のファイルを読めない（相対パスで渡す）

## 2026-10-10 注釈と manifest（iiif3d.py）・ビューアの改修

- 建物名と輪郭は OpenStreetMap（Overpass、2026-10-11T06:11Z 時点）。範囲内で名前のある建物は 21 件、
  店舗（ローソン）と範囲外が半分以上の建物を除いて 14 棟。PLATEAU の name は使えない（7 件のみ）
- 注釈の位置: 輪郭の中にあるセルの高さの上位 1 割 ＋ 3 m。カメラは建物ごとに南東上空（+45, +55, +75 m）から見下ろす。
  `scope` で注釈に結び、`behavior: ["hidden"]`
- Overpass の `out geom tags` はリレーション（中庭のある建物など）の形を返さない。`out geom` にする
- ビューア（iiif-3d-viewer、worktree `iiif-3d-viewer.worktrees/progressive-choice`、branch feat/progressive-choice）:
  paintedModels に `progressive` と `upgradeUrl`、Scene に裏読み込み（Upgrade）・点の大きさ・カメラを 1 回だけ合わせる修正。テスト 7 件通過
- 手元で確認: 軽い版が先に出て、高精細が届くと自動で差し替わる。中間は読まない。印を押すと見出しが出てカメラが寄る（out/blog/iiif-yasuda.jpg）
- 手元確認の落とし穴: Next.js の開発サーバは `127.0.0.1` で開くと画面の部品が動かない（allowedDevOrigins）。`localhost` で開く
- 出典: 点群は東京都「東京都デジタルツイン実現プロジェクト 区部点群データ」CC BY（G空間情報センター、計測 2023 年 3〜4 月）。建物名は OSM（ODbL）

## 2026-10-10 説明文（data/osm/descriptions.json）

- 公式資料（東京大学・部局のページ、広報誌「淡青」、キャンパスマップ、登録有形文化財の一覧）から事実だけを拾い、
  1〜2 文に書き直して出典の URL を付けた。原文の書き写しはしない
- 14 棟のうち 13 棟に説明。外したもの・削ったもの:
  - 化学西館: 旧称「化学新館」との対応が推定にとどまるので、名前だけにした
  - 山上会館の設計者（前川國男）: 出典が運営受託会社の発表資料だけなので削った
  - 情報学環本館の建築年（1983）: 大学の建物一覧の項目と同じ建物かが推定なので削った
  - 理学部1号館: OSM では中央棟・西棟が別の建物なので、説明を分けた（東棟は範囲内に無い）
- 手元のビューアで 14 件の一覧に説明と出典が出ることを確認
- 見出し・本文は日本語の body を先に置いている。ビューアは最初の本文だけを出すので、英語表示でも日本語が出る（未対応）

## 2026-10-11 公開（GitHub Pages）

- 置き場所は R2 ではなく、模型専用の公開リポジトリ nakamura196/hongo-campus-iiif（手元 `~/git/minecraft/hongo-campus-iiif`）の GitHub Pages にした。
  最大 19 MB で、1 ファイル 100 MB・サイト 1 GB の上限に収まる。作業用の本リポジトリは private のまま
- Git LFS は使わない（Pages は LFS の中身を配らない）。glb を作り直すと履歴が 1 回約 28 MB 増えるので、増えたら履歴を 1 コミットに戻す
- 配信を実測: `access-control-allow-origin: *`、glb は `content-type: model/gltf-binary`。Jekyll を通さないよう `.nojekyll` を置いた
- manifest は `--base https://nakamura196.github.io/hongo-campus-iiif/` で作り直した
- 本番の 3d.ldas.jp で開けた（https://3d.ldas.jp/ja/viewer?manifest=https%3A%2F%2Fnakamura196.github.io%2Fhongo-campus-iiif%2Fhongo.json）。点群と 14 個の印、メタデータ、密度の切り替えが出た。
  本番はビューア未改修なので、軽量版のまま差し替わらず、点は 1 画素で描かれてまばらに見える

## 2026-10-11 よりリアルにする材料の調査（PLATEAU LOD2 ほか）

- 本郷キャンパスの範囲（安田講堂周辺）は **PLATEAU 2025 年度版でも LOD1（箱）だけ**。PLATEAU VIEW のカタログ API で実測:
  文京区 3 次メッシュ 53394650 は maxLod 1、53394651 は maxLod 2 だが、LOD2 の建物は経度 139.7655 より東（湯島・池之端側）だけ。
  窓の東端は 139.7634 なので、窓の中の LOD2 は 0 棟
- 東隣の台東区は濃い: 53394652（上野駅周辺）maxLod 3、53394653 maxLod 4、53394642 maxLod 3
- LOD2 には写真が貼られている（53394651 で ParameterizedTexture 約 2,000 件）。写真は空中写真由来で、壁面を撮ったものではない（MLIT 技術資料 0062）。
  写真は CMS から 1 枚ずつ取れる: `<gml の URL のフォルダ>/53394651_bldg_6697_appearance/<名前>.jpg`。文京区の zip 全体は 637 MB
- ほかの候補:
  - 東京都「都市の3Dデジタルマップ」区部（2025-03-31 公開、https://catalog.data.metro.tokyo.lg.jp/dataset/t000008d2000000021）。LOD・写真の有無は未確認
  - 地理院タイル シームレス空中写真（屋根・地面の色付け用。出典表記で利用可）
  - Google Photorealistic 3D Tiles は不可（規約が保存・抽出・派生を禁止。https://developers.google.com/maps/documentation/tile/policies）
  - Mapillary は CC BY-SA 4.0 で、glb に継承が及ぶおそれ。後回し
  - 安田講堂の再配布できる 3D スキャンは見つからず
- 方針（本人の依頼）: LOD2 のある地区で、点群と PLATEAU LOD2（写真つき）を同じ範囲で作り、比較用に並べる。候補は上野駅周辺（53394652）

## 2026-10-11 Georeference（地図との対応）

- 3d.ldas.jp には地図と 3D を並べる別ページ `/ja/georef?manifest=...` が既にある（MapLibre、OpenFreeMap）。
  読むのは Scene.annotations の中の `motivation: ["georeferencing"]`、body は GeoJSON の FeatureCollection。
  各 Feature の `properties.resourceCoords` が模型上の x, y, z（シーン座標そのまま）。Georeference Extension の GCP（対応点）を 3D に広げた独自の使い方
- iiif3d.py に追加: 注釈を付けた建物ごとに対応点（経緯度 ↔ 注釈の位置）、`transformation: polynomial order 1`、
  範囲の外形を Manifest の `navPlace` に。@context は拡張を先、presentation を最後
- 手元で確認: 14 点が地図に出て、安田講堂は実際の位置（35.71343, 139.76230）に合った
- 地図ページで模型が粗く見える理由: (1) progressive の差し替えが無く軽量版のまま、(2) カメラが scope の隠しカメラを使わず、注釈の位置 × 1.5 に置く。どちらもビューア側の課題

## 2026-10-11 比較用: 上野駅周辺の点群と PLATEAU LOD2

- 範囲: EPSG:6677 で x=-5134〜-4878, y=-32100〜-31812（256 × 288 m、本郷と同じ大きさ）。点群は 1 枚（09LD0867）に収まる。
  3 次メッシュ 53394652 の中で、範囲内の建物数が多く LOD3 を 2 棟含む位置を総当たりで探した（上野駅の南、アメ横側）。
  高さの基準（base）は範囲内の地面の点（class 2）の 1 パーセンタイル = 1.99 m
- 53394652 の台東区 GML（142 MB）: 3,988 棟のうち LOD2 が 3,985、LOD3 が 3。写真の指定 16,014（画像 8,007 枚）。範囲内は 186 棟・写真 380 枚。
  写真は 1 枚 8 KB 前後と小さいが、斜めから撮った空中写真なので壁の窓まで見える（予想より良い）
- plateau_glb.py（新規）: 面を earcut で三角形に（mapbox_earcut 2.0.0 を pip --user で追加。shapely 2.0 には制約つき三角形分割が無い）。
  写真ごとに 1 材質（unlit）、写真の無い面は灰色。地面は PLATEAU に無いので、点群の地面の点から 1 m 格子の面を作り、点の色を付けた（建物の下は最寄りの値で埋める）
- 大きさ: PLATEAU 15.7 万三角形（建物 1.1 万＋地面）・写真 380 枚 → 11.1 MB、gltfpack `-c -kn -km` で 7.3 MB。点群は 27 万点 1.7 MB／79 万点 4.5 MB／379 万点 17.5 MB
- compare3d.py（新規）: 模型を Choice に並べる（progressive は付けない。段階ではなく別物なので）。対応点は範囲の四隅
- 見比べた印象（out/blog/ueno-plateau.jpg, ueno-points.jpg）: PLATEAU は面がそろい壁の写真で街らしく見える。点群は線路・電車・高架・細部が写る。
  PLATEAU の bldg には駅舎・高架・線路が無い（別の地物 tran・brid など）
- ライセンス: G空間情報センターの表記は「PLATEAU Site Policy『3. 著作権について』に拠る」。サイトポリシーは公共データ利用規約 1.0 準拠で、CC BY 4.0 と互換、加工した旨の明記が要る
- 公開リポジトリ（hongo-campus-iiif）に glb 4 つ・ueno.json・README を置いた。まだコミットしていない
- 本人の案（未着手）: ビューアに、2 つの模型を左右に並べ、拡大・回転を同期させる比較表示

## 2026-10-11 ビューアの説明欄の開閉と、比較ページ

- 説明欄（メタデータ・アノテーション）を開閉式にした（iiif-3d-viewer PR #15）。通常表示と地図ページで共通の部品。畳んだ状態はブラウザに保存し、ページをまたいで同じ。保存が無ければ幅 640px 未満で畳んで始める
- 比較ページ `/ja/compare?manifest=...&a=0&b=1&mode=side|swipe|spyglass` を作った（PR #16、#15 の上に積んだ）。manifest の最初の Choice から A と B を選ぶ
  - 地図のレイヤー比較にならい、左右に並べる・スワイプ・スパイグラスの 3 つにした（本人の提案）
  - 作り: 左右それぞれに WebGL の画面を 1 つずつ置き、操作している側のカメラをもう一方が毎フレーム写す。スワイプとスパイグラスは、B の画面を上に重ねて CSS の clip-path で切り抜く。切り抜きの外ではマウス操作が下の A に届くので、どちらを触っても同期する
  - 却下した案: 1 つの画面に 2 つの模型を入れ、描く範囲（scissor）や型抜き（stencil）で塗り分ける。読み込みは 1 回で済むが、丸い窓に stencil が要り、既存の Scene 部品をそのまま使えない
  - 上野（ueno.json）で、PLATEAU と点群の 3 つの見せ方を手元で確かめた。点群は線路と高架を写し、PLATEAU は壁の写真を持つ、という違いがスワイプでいちばん分かりやすい
- 手元で確かめていないこと: スマートフォン実機での操作（スパイグラスはマウスの位置に追従する作りなので、タッチでは指を置いた位置に出る）
- 同日、PR #15・#16 をマージして公開した。あわせて PR #17 で依存の脆弱性を直した。地図のライブラリ（maplibre-gl）は 6 系でしか直らなかった。6 系は地図の処理を別ファイルの worker で動かすため、そのファイルをビルド時に public/ へ写す必要があった。写さないと地図が真っ白になる（「Worker failed to load」）
