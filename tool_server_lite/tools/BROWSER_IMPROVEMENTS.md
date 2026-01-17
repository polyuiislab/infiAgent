# 浏览器工具反检测与人类行为模拟改进

## 改进概述

本次改进全面增强了浏览器工具的反检测能力和人类行为模拟，使其更难被网站的人机验证系统识别。

---

## 一、反检测改进（BrowserLaunchTool）

### 1. 启动参数优化
```python
# 添加的反检测启动参数
'--disable-blink-features=AutomationControlled'  # 禁用自动化控制特征
'--disable-features=IsolateOrigins,site-per-process'
'--disable-web-security'
'--no-sandbox'
'--disable-setuid-sandbox'
# ... 等更多参数
```

### 2. 浏览器指纹伪装
- **User-Agent**: 随机选择真实的浏览器 UA
- **语言设置**: zh-CN, zh, en
- **时区**: Asia/Shanghai
- **权限**: geolocation, notifications

### 3. JavaScript 注入反检测
```javascript
// 覆盖 navigator.webdriver（最关键）
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});

// 添加 Chrome 对象
window.chrome = { runtime: {} };

// 伪装 plugins、languages 等
```

**效果**: 浏览器看起来像普通用户的浏览器，而不是自动化工具。

---

## 二、人类行为模拟函数

### 1. `_random_delay(min_ms, max_ms)`
生成随机延迟，模拟人类操作的自然停顿。

### 2. `_generate_bezier_curve(start, end, steps)`
生成贝塞尔曲线路径，模拟真实的鼠标移动轨迹。

**特点**:
- 使用三次贝塞尔曲线
- 添加随机控制点
- 路径自然弯曲，不是直线

### 3. `_human_like_mouse_move(page, target_x, target_y)`
沿贝塞尔曲线移动鼠标到目标位置。

### 4. `_human_like_click(page, selector/x/y, button, delay_ms)`
模拟人类点击：
1. 移动鼠标到目标（带随机偏移）
2. 按下鼠标
3. 随机延迟 50-150ms
4. 释放鼠标
5. 点击后随机延迟

### 5. `_human_like_type(page, selector, text, delay_range)`
模拟人类输入：
- 逐字符输入
- 每个字符间随机延迟
- 10% 概率有更长停顿（模拟思考）

---

## 三、新增工具类

### 1. BrowserMouseMoveTool
**功能**: 鼠标移动到指定坐标

**参数**:
- `browser_id`: 浏览器会话ID
- `x`, `y`: 目标坐标
- `human_like`: 是否使用贝塞尔曲线移动（默认 True）

**使用场景**: 
- 悬停触发菜单
- 移动到特定位置准备点击

---

### 2. BrowserMouseClickCoordsTool
**功能**: 在指定坐标位置点击

**参数**:
- `browser_id`: 浏览器会话ID
- `x`, `y`: 点击坐标
- `button`: 鼠标按钮 ("left", "right", "middle")
- `click_count`: 点击次数（双击用2）
- `human_like`: 是否使用人类化点击（默认 True）

**使用场景**:
- 点击动态生成的元素（无法用选择器定位）
- 点击 Canvas 或 SVG 内的特定位置
- 右键菜单操作

---

### 3. BrowserDragAndDropTool
**功能**: 鼠标拖拽操作

**参数**:
- `browser_id`: 浏览器会话ID
- `from_x`, `from_y`: 起始坐标
- `to_x`, `to_y`: 目标坐标
- `human_like`: 是否使用人类化拖拽（默认 True）

**使用场景**:
- 滑动验证码（如滑块验证）
- 拖拽排序
- 拖拽文件上传
- 地图缩放/平移

**人类化拖拽流程**:
1. 移动到起点（贝塞尔曲线）
2. 随机延迟 100-200ms
3. 按下鼠标
4. 沿贝塞尔曲线移动到终点
5. 随机延迟 50-100ms
6. 释放鼠标

---

### 4. BrowserHoverTool
**功能**: 鼠标悬停操作

**参数**:
- `browser_id`: 浏览器会话ID
- `selector` 或 `(x, y)`: 悬停位置
- `duration_ms`: 悬停持续时间（默认 1000ms）
- `human_like`: 是否使用人类化移动（默认 True）

**使用场景**:
- 触发悬停菜单
- 查看 tooltip
- 触发延迟加载内容

---

## 四、改进现有工具

### 1. BrowserClickTool（改进）
**新增参数**:
- `human_like`: 是否使用人类化点击（默认 True）
- `button`: 鼠标按钮选择

**改进**:
- 点击前移动鼠标（贝塞尔曲线）
- 在元素中心附近随机偏移（±30%）
- 随机按下/释放延迟
- 点击后随机延迟

---

### 2. BrowserTypeTool（改进）
**新增参数**:
- `human_like`: 是否使用人类化输入（默认 True）
- `delay_range`: 字符间延迟范围（默认 50-150ms）

**改进**:
- 逐字符输入（而非直接填充）
- 每个字符间随机延迟
- 偶尔有更长停顿（模拟思考）

---

## 五、使用示例

### 示例1：处理滑块验证码
```python
# 1. 启动浏览器（自动反检测）
browser_launch(headless=False)

# 2. 导航到页面
browser_navigate(browser_id="xxx", url="https://example.com")

# 3. 拖动滑块（人类化拖拽）
browser_drag_and_drop(
    browser_id="xxx",
    from_x=100, from_y=200,  # 滑块起始位置
    to_x=300, to_y=200,      # 滑块目标位置
    human_like=True           # 使用贝塞尔曲线
)
```

### 示例2：人类化登录
```python
# 1. 点击用户名输入框（人类化点击）
browser_click(
    browser_id="xxx",
    selector="#username",
    human_like=True
)

# 2. 输入用户名（逐字符，随机延迟）
browser_type(
    browser_id="xxx",
    selector="#username",
    text="myusername",
    human_like=True,
    delay_range=(80, 200)  # 每个字符间延迟 80-200ms
)

# 3. 点击密码输入框
browser_click(
    browser_id="xxx",
    selector="#password",
    human_like=True
)

# 4. 输入密码
browser_type(
    browser_id="xxx",
    selector="#password",
    text="mypassword",
    human_like=True
)

# 5. 点击登录按钮
browser_click(
    browser_id="xxx",
    selector="button[type='submit']",
    human_like=True
)
```

### 示例3：悬停菜单
```python
# 悬停在菜单上触发下拉菜单
browser_hover(
    browser_id="xxx",
    selector="#menu-item",
    duration_ms=1500,
    human_like=True
)

# 点击子菜单项
browser_click(
    browser_id="xxx",
    selector="#submenu-item",
    human_like=True
)
```

---

## 六、反检测效果对比

### 改进前
```javascript
navigator.webdriver  // true ❌ 会被检测
window.chrome        // undefined ❌ 会被检测
navigator.plugins    // [] ❌ 会被检测
```

### 改进后
```javascript
navigator.webdriver  // undefined ✅ 正常浏览器
window.chrome        // {runtime: {}} ✅ 正常浏览器
navigator.plugins    // [1,2,3,4,5] ✅ 正常浏览器
```

### 行为对比

| 操作 | 改进前 | 改进后 |
|------|--------|--------|
| 鼠标移动 | 直线瞬移 ❌ | 贝塞尔曲线 ✅ |
| 点击位置 | 元素中心 ❌ | 中心附近随机 ✅ |
| 点击速度 | 瞬间按下释放 ❌ | 50-150ms延迟 ✅ |
| 输入速度 | 瞬间填充 ❌ | 逐字符+随机延迟 ✅ |
| 操作间隔 | 固定延迟 ❌ | 随机延迟 ✅ |

---

## 七、注意事项

1. **性能权衡**: 人类化操作会增加执行时间，如果不需要反检测，可以设置 `human_like=False`

2. **参数调整**: 可以根据需要调整延迟范围，例如：
   - 快速操作: `delay_range=(30, 80)`
   - 谨慎操作: `delay_range=(100, 300)`

3. **组合使用**: 对于高风险操作（如登录、支付），建议：
   - 使用 `human_like=True`
   - 在操作间添加额外的 `browser_wait` 延迟
   - 模拟真实用户的浏览行为（滚动、悬停等）

4. **检测升级**: 如果网站升级了检测机制，可能需要进一步调整：
   - 增加更多浏览器指纹伪装
   - 调整行为参数（延迟、路径等）
   - 添加更多随机性

---

## 八、技术细节

### 贝塞尔曲线算法
使用三次贝塞尔曲线公式：
```
B(t) = (1-t)³P₀ + 3(1-t)²tP₁ + 3(1-t)t²P₂ + t³P₃
```
其中：
- P₀: 起点
- P₁, P₂: 控制点（动态生成，添加随机性）
- P₃: 终点
- t: 0到1的参数

### 随机性来源
1. **路径随机**: 控制点位置随机偏移
2. **速度随机**: 每个路径点间的延迟随机
3. **位置随机**: 点击位置在元素内随机偏移
4. **时间随机**: 所有延迟都有随机范围

---

## 九、未来改进方向

1. **更多浏览器指纹**:
   - Canvas 指纹
   - WebGL 指纹
   - 字体指纹

2. **更复杂的行为模拟**:
   - 滚动行为（加速度、惯性）
   - 页面停留时间模拟
   - 阅读行为模拟

3. **机器学习轨迹**:
   - 收集真实用户的鼠标轨迹
   - 训练模型生成更逼真的轨迹

4. **验证码专用工具**:
   - 图片验证码识别
   - 滑块验证码自动求解
   - 点选验证码处理

---

## 十、总结

本次改进显著提升了浏览器工具的反检测能力：

✅ **反检测**: 隐藏自动化特征，伪装成真实浏览器  
✅ **人类化**: 模拟真实的鼠标轨迹和操作节奏  
✅ **灵活性**: 可选择是否启用人类化（性能 vs 安全）  
✅ **功能完整**: 支持点击、输入、拖拽、悬停等所有常见操作  

现在的浏览器工具可以更好地应对各种人机验证系统！

