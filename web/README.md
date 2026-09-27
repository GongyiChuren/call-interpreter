# web/

| 文件 | 说明 |
|---|---|
| `index.html` | 页面结构（就一个） |
| `app.js` | 软电话 + 翻译音频接管 —— 全部前端逻辑 |
| `style.css` | 样式，深色单页 |
| `worklets.js` | AudioWorklet：采集下采样 / 播放队列（内联注入，无独立文件） |
| `jssip.js` | **第三方**：JsSIP 3.13.8 的浏览器构建（MIT，https://github.com/versatica/JsSIP） |

`jssip.js` 是自建产物，因为 JsSIP 官方 npm 包不发布浏览器 UMD 包：

```bash
npm i jssip@3.13.8 esbuild
echo "import JsSIP from 'jssip'; window.JsSIP = JsSIP;" > entry.js
npx esbuild entry.js --bundle --format=iife --minify --target=es2020 \
  --define:global=globalThis --outfile=web/jssip.js
```

要么保留这个产物，要么自己接一个打包步骤 —— 页面只是个 `<script src>`，没有别的依赖。
