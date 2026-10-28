# 本地服务(ASO Bridge)接口约定

网页「一键发布」模块和用户本机上的本地服务之间的接口。网页只负责编辑和打包物料;App Store Connect 的 API 密钥(.p8)只存在本地服务里,网页拿不到。

模拟实现:`bridge-mock/mock_bridge.py`(只用 Python 标准库,不会真的连 App Store Connect)。

## 基本约定

- 地址:`http://127.0.0.1:47321`(端口可在网页「高级设置」里改)。
- **只监听 127.0.0.1**,不能监听 0.0.0.0。
- 校验 `Host` 头必须是 `127.0.0.1:<端口>` 或 `localhost:<端口>`,防 DNS rebinding。
- CORS 只放行白名单里的 Origin(正式域名 `https://xiaobai-aso.github.io` + 开发用的 `http://localhost:*`)。预检请求要回 `Access-Control-Allow-Private-Network: true`(Chrome 的 Private Network Access)。
- 除 `/health`、`/pair`、`/setup` 外,所有接口都要带 `Authorization: Bearer <token>`;`EventSource` 不能带请求头,改用 `?token=`。
- token 无效时回 `401`,网页会清掉 token 回到「未配对」状态。
- 出错统一回 JSON:`{"error": "给用户看的中文说明"}`。

## 配对

本地服务启动时在终端打印 6 位配对码(每次启动重新生成,用过即作废)。用户把配对码输进网页,换取长期 token。

```
POST /pair
{"code": "123456", "client": "苹果 ASO 工具"}
→ 200 {"token": "<随机 32 字节 hex>"}
→ 403 {"error": "配对码不对"}
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | `{"name": "aso-bridge", "version": "0.1.0"}`,网页用它检测服务是否在运行 |
| POST | `/pair` | 见上 |
| GET | `/setup` | 本地服务自己的设置页(HTML),添加 / 删除 API 密钥。网页只负责打开它 |
| GET | `/accounts` | `[{"id": "acc_1", "name": "我的公司", "issuerId": "…"}]`,不返回密钥内容 |
| GET | `/apps?account=` | `[{"id": "6470000000", "name": "记账本", "bundleId": "com.x.ledger"}]` |
| GET | `/apps/{appId}/versions?account=` | `[{"id": "…", "versionString": "1.2.0", "platform": "IOS", "state": "PREPARE_FOR_SUBMISSION", "stateLabel": "准备提交", "editable": true}]` |
| GET | `/apps/{appId}/metadata?account=&version=` | 后台现有文案,网页「一键导入 → 从 App Store Connect 拉取」用,格式见下 |
| POST | `/jobs?account=&app=&version=` | 请求体是发布包 zip(`Content-Type: application/zip`),回 `{"id": "job_xxx"}` |
| GET | `/jobs/{id}/events?token=` | SSE 进度流,见下 |

`editable` 为 `false` 的版本(审核中、已上架)网页会标成「不可编辑」。

`/metadata` 返回指定版本的全部语言(`version` 为空时用最新的可编辑版本,没有就用线上版本):App 信息级字段来自 `appInfoLocalizations`,其余来自 `appStoreVersionLocalizations`,审核信息来自 `appStoreReviewDetail`。

```json
{
  "locales": {
    "zh-Hans": {"name": "…", "subtitle": "…", "privacyPolicyUrl": "…", "keywords": "…", "promotionalText": "…", "description": "…", "whatsNew": "…", "supportUrl": "…", "marketingUrl": "…"}
  },
  "review": {"contactFirstName": "", "contactLastName": "", "contactPhone": "", "contactEmail": "", "demoAccountRequired": false, "demoAccountName": "", "demoAccountPassword": "", "notes": ""}
}
```

## 发布包(aso-release/1)

`POST /jobs` 和「下载 asc 发布包」是同一个 zip,本地服务按 `manifest.json` 执行,其余文件是给 `publish.sh`(asc 脚本)用的。

```
aso-release-<appId>-<version>/
├── manifest.json
├── publish.sh
├── README.txt
├── screenshots/<设备类型>/<语言>/01.png …     成品图可能是 .jpg;某个语言没有图时整个文件夹不存在
├── metadata/version/<语言>.strings        description / keywords / whatsNew / promotionalText / supportUrl / marketingUrl
├── metadata/descriptions/<语言>.txt       新建语言时用
├── metadata/app-info/<语言>/{name,subtitle,privacy_url}.txt
└── review/{contact_email,contact_phone,contact_name,notes,demo_account,demo_password}.txt
```

`manifest.json`:

```json
{
  "format": "aso-release/1",
  "app": {"appId": "6470000000", "version": "1.2.0", "firstRelease": false},
  "parts": {"shots": true, "meta": true, "review": true},
  "locales": ["zh-Hans", "en-US"],
  "replaceScreenshots": true,
  "screenshotSource": "design",
  "screenshots": {"IPHONE_67": {"size": "1320x2868", "pages": 5}},
  "metadata": {
    "zh-Hans": {"name": "…", "subtitle": "…", "privacyPolicyUrl": "…", "promotionalText": "…", "keywords": "…", "description": "…", "whatsNew": "…", "supportUrl": "…", "marketingUrl": "…"}
  },
  "review": {"contactEmail": "dev@example.com", "notes": "…"}
}
```

**没填 = 沿用后台**:`metadata` 只包含填了的语言和字段,`review` 只包含填了的项(`demoAccountRequired` 只在开启演示账号时出现,值为 `true`)。本地服务只更新出现的字段,缺的字段、缺的语言一律不动 App Store Connect 里现有的内容;`review` 为 `{}` 时跳过审核信息。zip 里对应的文件也只在有内容时才写。

`screenshotSource` 是 `design`(工具里设计、按尺寸生成)或 `ready`(导入的成品图)。成品图时 `screenshots` 改成按语言计数,同一设备类型下可能混有苹果认可的几种像素尺寸:

```json
"screenshots": {"IPHONE_67": {"sizes": ["1320x2868", "1290x2796"], "counts": {"zh-Hans": 3, "en-US": 0}}}
```

本地服务以 `screenshots/` 里实际的文件为准,计数为 0 的语言直接跳过(App Store 会显示主语言的截图)。

设备类型对应 App Store Connect 的 `screenshotDisplayType`(去掉 `APP_` 前缀):`IPHONE_67`(6.9″)、`IPHONE_65`、`IPHONE_61`(6.3″)、`IPAD_PRO_3GEN_129`(13″)。

### 本地服务执行顺序

1. 按 `app.version` 找到版本 ID,版本不存在或不可编辑就报错。
2. 截图:每个设备类型 × 语言,`replaceScreenshots` 为真时先删掉旧截图,再按文件名顺序走「预约 → 上传分片 → 提交校验和」流程。
3. App 信息:`appInfoLocalizations` 更新 name / subtitle / privacyPolicyUrl,没有的语言新建。
4. 版本文案:`appStoreVersionLocalizations` 更新其余字段,没有的语言新建。
5. 审核信息:`appStoreReviewDetails`,没有就新建。
6. **不提交审核。**

## SSE 事件

每条都是 `data: <JSON>`(不用自定义 event 名):

```
data: {"type":"step","id":"shots-IPHONE_67-zh-Hans","label":"上传截图 iPhone 6.9″ · 简体中文(5 张)","status":"run"}
data: {"type":"step","id":"shots-IPHONE_67-zh-Hans","label":"上传截图 iPhone 6.9″ · 简体中文(5 张)","status":"done"}
data: {"type":"progress","pct":40,"msg":"正在上传截图 4 / 10"}
data: {"type":"log","msg":"跳过 marketingUrl:没填"}
data: {"type":"done","msg":"全部上传完成"}
data: {"type":"error","msg":"版本 1.2.0 正在审核中,不能修改"}
```

- `step` 用 `id` 去重:同一个 `id` 先发 `run` 再发 `done` / `error`,网页会原地更新那一行。
- `pct` 是 0~100。
- 收到 `done` 或 `error` 后网页关闭连接;服务端发完也应关闭。
- 网页断线重连时,服务端应从头重放这个 job 的全部事件。
