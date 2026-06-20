== Gmail 邮件查看器 使用说明 ==

【一键启动】
  双击 start.bat
  - 首次启动会自动生成 .env 并打开记事本编辑，请把
        APP_ACCESS_TOKEN=change-me
    改成你自己的访问密码，保存关闭后启动会继续。
  - 启动后会自动打开浏览器：http://127.0.0.1:8000

【放置 Gmail 授权文件】
  按下面结构放好已授权的 token.json / credentials.json：
    gmail_credentials\gmail1\credentials.json
    gmail_credentials\gmail1\token.json
    gmail_credentials\gmail2\credentials.json
    gmail_credentials\gmail2\token.json
    gmail_credentials\gmail3\credentials.json
    gmail_credentials\gmail3\token.json

【关闭】
  双击 stop.bat

【常见问题】
  1) Windows Defender / 360 / 火绒 报毒：手动加白名单。
  2) 浏览器提示 token 错误：把 .env 里的 APP_ACCESS_TOKEN 设为强密码再用。
  3) 端口被占用：编辑 .env 把 PORT=8000 改成其它端口。
