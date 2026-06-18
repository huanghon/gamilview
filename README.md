# Gmail 邮件内容聚合查看网页/API

这是一个本地自用工具，用于读取自己已授权的 Gmail 邮箱，并按手机号或标识匹配最新邮件，提取其中的数字验证码。

## 安装

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
```

修改 `.env`：

```env
APP_ACCESS_TOKEN=换成你自己的访问token
GMAIL_CREDENTIALS_DIR=./gmail_credentials
HOST=127.0.0.1
PORT=8000
```

## 配置手机号/标识

编辑 `config/phones.json`：

```json
[
  {
    "phone": "70200038",
    "record_url": "http://154.17.167.99/api/v1/smpp/record?token=xxx&format=txt2",
    "gmail_accounts": ["gmail1", "gmail2", "gmail3"],
    "keywords": ["70200038"],
    "enabled": true
  }
]
```

系统会用类似 `newer_than:30d 70200038` 的 Gmail 搜索语句读取邮件。也可以给单个配置增加 `gmail_query` 字段覆盖默认搜索。

## Gmail 文件放置方式

推荐目录结构：

```text
gmail_credentials/
  gmail1/
    credentials.json
    token.json
  gmail2/
    credentials.json
    token.json
  gmail3/
    credentials.json
    token.json
```

也支持平铺文件：

```text
gmail_credentials/
  gmail1_credentials.json
  gmail1_token.json
  gmail2_credentials.json
  gmail2_token.json
  gmail3_credentials.json
  gmail3_token.json
```

如果还没有 `token.json`，先放好 Google Cloud 下载的 OAuth 客户端文件，然后执行：

```powershell
python -m gmail.client authorize gmail1 --dir .\gmail_credentials
python -m gmail.client authorize gmail2 --dir .\gmail_credentials
python -m gmail.client authorize gmail3 --dir .\gmail_credentials
```

浏览器完成授权后会生成对应账号的 token 文件。

## 启动

```powershell
python app.py
```

访问：

```text
http://127.0.0.1:8000/
```

首页可以输入手机号和对应 Gmail 账号别名，例如：

```text
70200038
70200038 gmail1
60401656 gmail2
```

如果只填写手机号，不填写邮箱账号，系统会按 `gmail1`、`gmail2`、`gmail3` 的顺序依次搜索标题中包含该手机号的最新邮件。

第二列也可以直接填写邮箱地址，系统会自动映射到对应账号别名：

```text
ehdqja9179@gmail.com => gmail1
magic22dan@gmail.com => gmail2
chlqlrkfdl@gmail.com => gmail3
```

例如：

```text
15663355 gmail1
15663355 ehdqja9179@gmail.com
70200750 magic22dan@gmail.com
```

点击生成后会得到专用链接：

```text
70200038----http://127.0.0.1:8000/api/v1/smpp/record?token=自动生成token&format=txt2
```

访问这个链接时，系统会用该 token 对应的 Gmail 账号搜索标题中包含该手机号的最新邮件，并以纯文本返回邮件正文中提取出的验证码。

## API

```text
POST /api/record-links
GET  /api/v1/smpp/record?token=xxx&format=txt2
GET  /api/v1/smpp/record?token=xxx&format=json
GET  /api/phones?token=xxx
GET  /api/mail/70200038?token=xxx
GET  /api/mails/70200038?token=xxx
GET  /api/mail-detail/{message_id}?phone=70200038&token=xxx
POST /api/refresh?phone=70200038&token=xxx
POST /api/refresh?token=xxx
GET  /health
```

## 注意

- 这个工具只应用于你自己已授权的 Gmail 邮箱。
- 不要把 `.env`、`token.json`、`credentials.json` 上传或发给别人。
- 默认只监听 `127.0.0.1`。如果确实要公网访问，再把 `.env` 里的 `HOST` 改为 `0.0.0.0`，并自行配置防火墙、反向代理和 HTTPS。
