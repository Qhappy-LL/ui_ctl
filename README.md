# ui_ctl

ui_ctl 是基于 ADB 的单文件 Android UI 操控库，无需安装额外 APK 或部署设备端 agent。支持元素定位、点击、滑动、滚动查找、截图及可选模板匹配，默认拒绝歧义点击，并显式报告命令失败。

说白了，就是用几行 Python 让手机点按钮、滑页面、输文字，手机上不用再装一套控制软件。

## 特点

- **零设备端安装**：不部署额外 APK、设备端 agent 或输入法。执行时仍会启动设备端命令进程，`uiautomator dump` 会建立临时 UiAutomation 连接；不承诺无痕或规避检测（[AOSP dump 实现](https://android.googlesource.com/platform/frameworks/uiautomator/+/dec12c69b4e09679f691050d8fdcab0a5e474258/cmds/uiautomator/src/com/android/commands/uiautomator/DumpCommand.java)）。
- **保守语义，遇到歧义就报错**：
  - 点击、长按未显式指定 index 时，选择器匹配到多个元素报 `AmbiguousElementError`，不默认点第一个
  - 不可见、被父容器裁剪、零面积、小于 `MIN_VISIBLE_SIZE` 的节点不参与匹配（部分 ROM 的 WebView 会把屏幕外节点压成 2 至 3 像素细条）
  - ADB 非零退出、设备报 `SecurityException` 直接抛异常，不把失败当成功
  - 输入注入不自动重试，避免重复操作
- **基础功能纯标准库**：单文件 `ui_ctl.py` 拷贝即用。图像模板识别是可选功能，需要 OpenCV / NumPy。

## 环境要求

- Python 3.10 或以上
- Android 设备开启 USB 调试，`adb devices` 能看到
- 动作默认经 `su -c input` 注入，需要 root。这是针对已验收 MIUI 环境的配置；如果目标 ROM 允许 shell 直接运行 `input`，把配置区 `SU_INPUT` 改为 `False` 即可免 root 使用。

## 配置

所有可配项在脚本顶部配置区，改完直接跑：

```python
SERIAL = "your-device-serial"  # 改成 adb devices 列出的序列号
```

## 用法

```python
import ui_ctl as u

u.tap(text="设置", exact=True)
assert u.wait_for(rid="com.android.settings:id/scroll_headers",
                  exact=True, timeout=6)

# 坐标为当前方向的整屏像素，不做裁剪或去状态栏的隐式换算。
width, height = u.display_size()
u.swipe(width // 2, int(height * .75), width // 2, int(height * .35), ms=400)
u.drag(120, 900, 950, 900, ms=1600)
u.long_press(text="目标文字", exact=True, ms=1200)

el = u.scroll_to(text="更多设置", exact=True, max_swipes=5)
u.tap(text="更多设置", exact=True)

# 多个滚动区时显式选容器；方向描述手指移动方向。
el = u.scroll_to(text="目标条目", exact=True,
                 container={"rid": "列表ID", "exact": True}, direction="up")
els = u.dump_ui()
matches = u.find_all(els, rid="android:id/title", exact=True)
u.tap(rid="android:id/title", exact=True, index=1)

# 从缩放后的整屏截图定位时，先还原到手机像素。
point = u.map_point(100, 200, image_size=(540, 1124))
u.tap(*point)

u.screenshot("out/ui/current.png")
u.screen_state()  # locked 为 True / False / None，None 表示无法确定

# 不自动提交。默认收起软键盘并逐字注入，verify 核对输入框全文。
u.input_text("hello", verify={"rid": "输入框ID", "exact": True})
# 已确认兼容的场景可 char_delay=None 使用批量输入。

# 模板来自相同分辨率、缩放、主题和方向。
match = u.find_image("out/ui/target.png", threshold=0.98)
u.tap_image("out/ui/target.png", region=(0, 100, 1080, 1800))
```

## API

| 函数 | 作用 |
|---|---|
| `dump_ui(save_raw=None)` | 取新元素树，含可见区域、父容器裁剪、父子关系 |
| `find` / `find_all` | 元素查找，支持 text / rid / cls / desc / pkg / within / clickable_only；默认仅匹配可见节点 |
| `tap` / `long_press` | 点击 / 长按，坐标与选择器二选一；选 index 时按序取第 N 个 |
| `swipe` / `drag` | 滑动 / 系统长按拖拽（`ms >= 1200`，见下方限制） |
| `keyevent(code)` | 按键，如 `BACK`、`HOME`；`224` 唤醒屏幕 |
| `input_text(s, verify=...)` | 文本输入，仅可打印 ASCII；verify 指定输入框并核对全文 |
| `wait_for(text=, rid=, desc=, timeout=)` | 等待元素出现，超时预算含 dump |
| `scroll_to(text=, direction=, max_swipes=, container=)` | 在滚动容器内查找；到底或预算耗尽返回 None |
| `display_size()` | 当前方向的整屏像素宽高 |
| `screenshot(path=None)` | 截图，校验 PNG CRC / IEND / 像素长度后才保存 |
| `map_point(x, y, image_size)` | 缩放后的整屏截图坐标换算为设备坐标 |
| `find_image(template, threshold=, region=)` / `tap_image(...)` | 同尺度模板匹配 / 识别后点击 |
| `screen_state()` | 亮屏状态、前台窗口、锁屏判断（True / False / None） |
| `wake()` / `unlock(pin=)` | 唤醒 / 解锁；至多尝试一次，不猜密码、不重试 |

## 行为约定

- 点击、长按未指定 index 时要求唯一匹配；重复匹配抛 `AmbiguousElementError`。
- 选择器默认包含匹配，精准目标优先 `exact=True`；rid 同样受 exact 控制。
- 动作始终重新获取元素树，避免旧树坐标误点；传入的旧 `ui` 参数仅为调用兼容保留。
- tap / long_press 要求元素启用；不要求文字节点本身 clickable，点击其可见中心可交给父容器处理。
- 越界坐标、选择器拼写错误、空选择器、负索引、混用坐标与选择器均报错。
- `wait_for` 的 timeout 包含 dump 与 pull，使用单调时钟；正常缺失或期限耗尽返回 None，设备故障抛异常。
- `scroll_to` 按可见滚动容器计算坐标，支持四个方向、到边界停止；多个容器时请传 `container`，避免选错列表。
- 截图直传失败后转设备临时文件 + pull，不保存半截图。
- dump 使用唯一设备临时文件，备用截图流程也会创建设备临时文件；清理尽力执行，超时或断连时可能残留。这不代表同一手机可以被多个控制端同时操作，动作仍应串行。
- `input_text` 只接受可打印 ASCII。部分 ROM 输入法会改写可见键盘下的键事件，默认先确认 `mInputShown` 再收起键盘。关键输入须传 verify，输入前确认目标唯一、启用且聚焦，输入后核对全文，是否提交由调用方决定。
- `unlock(pin=...)` 只在确认锁屏且识别到唯一系统 PIN 框时尝试一次，不猜测 PIN、不追加已有内容、失败不重试。
- 动作返回坐标只证明注入调用完成，不等于业务成功；后续应核对页面元素或可观察的业务结果。

## 验收与限制

验收环境：MI 8 / Android 10 / MIUI 12.5.2，竖屏 1080×2248，横屏 2248×1080。

- 设置页：按文字进入，定位并滚动到「更多设置」，点击进入后核对页面内容，通过。
- 本地验收页：18 次点击覆盖 6 个位置，元素定位、坐标、半尺寸截图换算三种方式全部命中指定目标。页面 click 事件相对按钮中心的最大偏差约 3.69 手机像素；包含浏览器整数 CSS 坐标量化，不能当作硬件触摸传感器精度。
- 横屏：截图尺寸与 `display_size()` 一致，元素点击收到目标页事件回传。
- 水平 / 垂直滑动、1100ms 长按、1600ms 系统拖拽，以及列表第 1 项到第 20 项再返回，通过事件轨迹或内容变化验证。
- 本地图像模板识别到唯一目标，点击后收到目标回传；重复模板和缺失模板有回归测试。
- 通知栏不误判锁屏；截断 PNG 的真实备用获取流程、断连、歧义拒绝均验证。
- ASCII URL 和包含空格、`%s`、引号、分号、管道等字符的输入已在隐藏键盘后核对通过。

**系统拖拽限制**：Android 10 的 draganddrop 把起始长按计入总时长，首个 MOVE 已位于路径中段。实测 650ms 没有 MOVE；1600ms 约 770ms 后首次移动，终点正确。因此默认 1600ms，拒绝小于 1200ms 的拖拽。它适合一般拖放，不承诺连续匀速轨迹，不能用来精细描线。

**识别限制**：元素树识别依赖 App 提供可访问节点。图像识别是同尺度模板匹配，不是 OCR 或通用视觉理解；主题、尺寸、动画变化时应重新准备模板。动态页面在识别与注入之间仍可能变化，关键操作必须验证后置结果。不覆盖多指手势或多显示器控制。

## 回归测试

```bash
python -X utf8 -m unittest test_ui_ctl -v
```

31 个用例全部基于 mock，不需要连接设备。图像匹配用例在未安装 OpenCV 时自动跳过。

## License

MIT
