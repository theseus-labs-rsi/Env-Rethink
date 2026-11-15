# OSWorld 1\.0 环境加强设计

# 任务介绍

|\#|领域|任务 ID|测试点内容|原始评分方法|
|---|---|---|---|---|
|1|chrome|bb5e4c0d\-f964\-439c\-97b6\-bdb9747de3f4|将 Chrome 的默认搜索引擎设置为 Bing。|`match_in_list`：读取默认搜索引擎，名称必须为 `Microsoft Bing` 或 `Bing`。|
|2|chrome|2ad9387a\-65d8\-4e33\-ad5b\-7580065a27ca|在 Chrome 书签栏创建名为 `Favorites` 的文件夹。|`is_expected_bookmarks`：书签栏直属文件夹的名称集合必须恰好等于 `{Favorites}`。|
|3|chrome|030eeff7\-b492\-4218\-b312\-701ec99ee0cc|在 Chrome 中启用 Do Not Track。|`exact_match`：读取 Do Not Track 状态，必须等于字符串 `true`。|
|4|gimp|d52d6308\-ec58\-42b7\-a2c9\-de80e4837b2b|移除 GIMP 左侧停靠栏。|`check_config_status`：读取 GIMP `sessionrc`，要求 `hide-docks=yes`。|
|5|gimp|7b7617bd\-57cc\-468e\-9c91\-40c4ec2bcb3d|将 GIMP 最小撤销步数设置为 100。|`check_config_status`：读取 GIMP `gimprc`，要求 `undo-levels=100`。|
|6|gimp|7767eef2\-56a3\-4cea\-8c9f\-48c070c7d65b|将 GIMP 主题从深色改为浅色。|`check_config_status`：读取 GIMP `gimprc`，要求 `theme="Light"`。|
|7|libreoffice\_calc|1273e544\-688f\-496b\-8d89\-3e0f40aa0606|将 `Revenue` 列及表头复制到新的 `Sheet2` 工作表。|`compare_table`：读取 `/home/user/NetIncome.xlsx`；`Sheet2` 数据须匹配标准文件 `3_NetIncome_gt1.xlsx` 的 `Sheet2`，并通过 `preserve_cells` 保持 `Sheet1` 单元格与标准文件一致。两项都通过才得 1。|
|8|libreoffice\_calc|3aaa4e37\-dc91\-482e\-99af\-132a612d40f3|把当前工作表按屏幕显示内容导出为 CSV，其他导出选项不变，文件名与原 XLSX 相同。|`compare_csv`：`/home/user/Export_Calc_to_CSV.csv` 必须与标准 CSV 逐行一致，默认采用严格、区分大小写的比较。|
|9|libreoffice\_calc|7efeb4b1\-3d19\-4762\-b163\-63328d66303b|在 `Seq No.` 列按 `No. #` 格式填充序号，不改动无关区域。|`compare_table`：读取 `/home/user/Order_Sales_Serial#.xlsx`；第一张工作表数据须与 `Order_Sales_Serial#_gold.xlsx` 第一张表一致。表格数据比较默认先将数值舍入至 4 位小数。|
|10|libreoffice\_impress|ef9d12bd\-bcee\-4ba0\-a40e\-918400f43ddf|恢复 Impress 左侧幻灯片面板。|`check_left_panel`：无障碍树中须存在名称为 `Slides View` 的 `document-frame` 节点。|
|11|libreoffice\_impress|ce88f674\-ab7a\-43da\-9201\-468d38539e4a|将 Impress 幻灯片从横向改为纵向。|`check_slide_orientation_Portrait`：读取 `AM_Last_Page_Template.pptx`，幻灯片宽度必须小于高度。|
|12|libreoffice\_impress|21760ecb\-8f62\-40d2\-8d85\-0cee5725cb72|为 Impress 第一页添加 `dissolve` 切换效果。|`check_transition`：读取 `Ch4 Video Effect.pptx`，第 1 张幻灯片的 XML 中须存在 `dissolve` 切换元素。|
|13|libreoffice\_writer|3ef2b351\-8a84\-4ff2\-8724\-d86eae9b842e|将 Writer 文档标题居中对齐。|`is_first_line_centered`：读取 `Constitution_Template_With_Guidelines.docx`，第一个段落的对齐属性必须为 CENTER。|
|14|libreoffice\_writer|0e763496\-b6bb\-4508\-a427\-fad0b6c3e195|将 Writer 全文字体改为 `Times New Roman`。|`compare_font_names`：读取 `Dublin_Zoo_Intro.docx`，正文所有段落中每个文本片段的显式字体名都须为 `Times New Roman`。|
|15|libreoffice\_writer|ecc2413d\-8a48\-416e\-a3a2\-d30106ca36cb|在 Writer 当前页面之后插入空白页。|`contains_page_break`：读取 `Sample_Statutory_Declaration.docx`，正文中的显式分页符总数必须恰好为 5。|
|16|multi\_apps|2b9493d7\-49b8\-493a\-a71b\-56cd1f4d6908|在 Ubuntu 中通过命令行强制退出卡住的 Writer，不重启电脑、不影响其他工作。|`check_include_exclude`：任务命令检查 `soffice` 进程和 `~/.bash_history` 中的 `kill` 记录；输出须同时包含 `no libreoffice is running`、`killed from terminal`，且不含相反状态。|
|17|multi\_apps|937087b6\-f668\-4ba6\-9110\-60682ee33441|将 Ubuntu 默认视频播放器设置为 VLC。|`check_include_exclude`：读取默认视频播放器配置，结果须包含 `vlc.desktop`。|
|18|multi\_apps|510f64c8\-9bcc\-4be1\-8d30\-638705850618|从终端在 `~/Desktop/project` 文件夹中启动 VS Code。|`check_include_exclude` \+ `compare_config`：`~/.bash_history` 须有包含 `code ` 的记录；VS Code 扩展导出的 `OpenProject.txt` 须包含 `project`。原任务按 AND 组合，两项都通过才得 1。|
|19|os|f9be0997\-4b7c\-45c5\-b05c\-4612b44a6118|在 Ubuntu 中启用勿扰模式。|`exact_match`：`gsettings get org.gnome.desktop.notifications show-banners` 输出须严格等于 `false\n`。|
|20|os|28cc3b7e\-b194\-4bc9\-8353\-d04c0f4d56d2|将系统输出音量调到最大。|`exact_match`：原任务命令提取第一个 PulseAudio 音频输出的音量百分比，结果须严格等于 `100\n`。|
|21|os|e0df059f\-28a6\-4169\-924f\-b9623e7184cc|将桌面目录 `todo_list_Jan_1` 重命名为 `todo_list_Jan_2`。|`exact_match`：`~/Desktop/todo_list_Jan_2` 必须存在且 `~/Desktop/todo_list_Jan_1` 必须不存在，检查命令须输出 `Directory exists.\n`。|
|22|thunderbird|a10b69e1\-6034\-4a2b\-93e1\-571d45194f75|在 Thunderbird 创建 `COMPANY`、`UNIVERSITY` 两个本地文件夹。|`check_list`：本地文件夹列表须同时匹配 `COMPANY`、`UNIVERSITY` 文件夹及各自的 `.msf` 文件，共 4 项正则条件。|
|23|thunderbird|10a730d5\-d414\-4b40\-b479\-684bed1ae522|在 Thunderbird 中启用完整深色模式。|`check_thunderbird_prefs`：读取账户配置 `prefs.js`，`extensions.activeThemeID` 须匹配正则 `dark`。|
|24|thunderbird|3f28fe4f\-5d9d\-4994\-a456\-efd78cfae1a3|为 Thunderbird 邮件账户设置纯文本签名：第一行 `Anonym`，第二行 `XYZ Lab`。|`check_thunderbird_prefs`：读取 `prefs.js`，`mail.identity.id1.htmlSigText` 须匹配包含换行的 `Anonym\nXYZ Lab`。|
|25|vlc|8d9fd4e2\-6fdb\-46b0\-b9b9\-02f06495c62f|启用 VLC 全屏模式，使视频填满整个屏幕。|`is_vlc_fullscreen`：VLC 窗口宽、高须为正整数，并分别等于屏幕宽、高；原检查采用窗口尺寸比较。|
|26|vlc|9195653c\-f4aa\-453d\-aa95\-787f6ccfaae9|将 VLC 可调最大音量从 125% 提高为原始音量的 200%。|`check_qt_max_volume`：读取 VLC `vlcrc`，要求 `qt-max-volume=200`。|
|27|vlc|8ba5ae7a\-5ae5\-4eab\-9fcc\-5dd4fe3abf89|将 VLC 录制内容保存目录改为 Desktop。|`is_vlc_recordings_folder`：读取 VLC `vlcrc`，未注释的 `input-record-path` 须等于 `/home/user/Desktop`。|
|28|vs\_code|982d12a5\-beab\-424f\-8d38\-d2a48429e511|将 VS Code 颜色主题改为 `Visual Studio Dark`。|`compare_config`：读取 `/home/user/.config/Code/User/settings.json`，要求包含 `workbench.colorTheme="Visual Studio Dark"`，允许保留其他设置。|
|29|vs\_code|276cc624\-87ea\-4f08\-ab93\-f770e3790175|将 VS Code 当前用户的代码换行列宽设置为 50 个字符。|`check_json_settings`：读取 User 范围 `/home/user/.config/Code/User/settings.json`，要求 `editor.wordWrapColumn=50`。|
|30|vs\_code|70745df8\-f2f5\-42bd\-8074\-fbc10334fcc5|在 VS Code 启用自动保存，并将保存延迟设置为 500 毫秒。|`check_json_settings`：读取相同 User 设置文件，要求同时具有 `files.autoSave="afterDelay"`、`files.autoSaveDelay=500`。|

# 加强设计和得分

|模型|baseline均分|加强后均分|降低/持平/升高|
|---|---|---|---|
|`DeepSeek-V4.1-Flash`|0\.8000|0\.1667|19/11/0|
|`glm-5.3-flash-cc`|0\.8667|0\.4333|14/15/1|
|`qwen3.8-flash`|0\.1000|0\.0000|3/27/0|

|\#|测试点|最终综合加强设计|完成原任务的操作路径|DeepSeek baseline|DeepSeek 加强后|GLM baseline|GLM 加强后|Qwen baseline|Qwen 加强后|
|---|---|---|---|---|---|---|---|---|---|
|1|将 Chrome 的默认搜索引擎设置为 Bing。|保留文本缩放2\.0和鼠标尺寸48，将Chrome定位到搜索引擎管理页的站内搜索区域；默认搜索引擎目标不预先修改。|关闭其他前台窗口，在Chrome设置中选择默认搜索引擎，而非添加站内搜索。|1\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|2|在 Chrome 书签栏创建名为 `Favorites` 的文件夹。|在其他书签中创建Favorites诱饵文件夹；书签栏增加指向其他书签页的同名URL链接但不创建目标文件夹；展开其他书签范围并放大浏览器页面。 另将独立配置档编辑器置于前台，其Bookmarks bar folder显示Favorites，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，核对书签栏同名项目的类型，创建真正的Favorites文件夹。|1\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|3|在 Chrome 中启用 Do Not Track。|将Chrome定位到广告隐私设置页；保留文本缩放2\.0，使浏览器全局Do Not Track不在默认视图。|通过地址栏进入全局隐私设置，再找到Do Not Track并启用。|1\.0|0\.0|1\.0|1\.0|0\.0|0\.0|
|4|移除 GIMP 左侧停靠栏。|保留文本缩放3\.0，打开GIMP首选项并进入窗口管理；Tab默认作用于对话框控件，真正的Hide Docks需要返回主图像窗口操作。 另将独立配置档编辑器置于前台，其Hide docks显示True，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，关闭首选项，切回GIMP主窗口，通过Windows菜单或Tab隐藏停靠栏。|1\.0|1\.0|1\.0|1\.0|0\.0|0\.0|
|5|将 GIMP 最小撤销步数设置为 100。|保留文本缩放3\.0，打开GIMP的Undo History图像历史面板；不修改全局最小撤销步数，历史面板不是持久设置界面。 另将独立配置档编辑器置于前台，其Minimum undo steps显示100，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，进入Preferences的System Resources，设置最小撤销步数为100并保存。|1\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|6|将 GIMP 主题从深色改为浅色。|文本缩放3\.0，打开GIMP首选项并选中Icon Theme而非Theme；桌面主题及GIMP主题目标保持原值。|切换至Theme页，选择Light并保存。|1\.0|0\.0|0\.0|1\.0|0\.0|0\.0|
|7|将 `Revenue` 列及表头复制到新的 `Sheet2` 工作表。|保持Sheet1全部单元格值不变，隐藏Revenue列，并预选相邻可见列；文本缩放2\.0。|在Calc中显示隐藏列，复制Revenue及表头至Sheet2，保持Sheet1数据不变。|0\.0|0\.0|1\.0|1\.0|0\.0|0\.0|
|8|把当前工作表按屏幕显示内容导出为 CSV，其他导出选项不变，文件名与原 XLSX 相同。|保留文本缩放2\.0，打开原工作簿的PDF导出设置对话框；原XLSX及CSV期望值保持不变。|取消PDF导出，使用另存为CSV并保留任务要求的导出选项。|1\.0|0\.0|1\.0|1\.0|0\.0|0\.0|
|9|在 `Seq No.` 列按 `No. #` 格式填充序号，不改动无关区域。|保持原工作表数据不变，隐藏数据区的交替行并预选Seq No\.列，文本缩放2\.0。 另将独立配置档编辑器置于前台，其Seq No\. format显示No\. \#，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，取消隐藏后对完整数据区填写序号，不只填写默认可见行。|0\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|10|恢复 Impress 左侧幻灯片面板。|保留原Impress左侧幻灯片面板隐藏状态，文本缩放3\.0。 另将独立配置档编辑器置于前台，其Slide panel visible显示True，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，通过View菜单恢复真正的左侧Slide Pane。|1\.0|0\.0|1\.0|1\.0|0\.0|0\.0|
|11|将 Impress 幻灯片从横向改为纵向。|保留文本缩放2\.0，打开打印设置对话框，展示打印方向控制；演示文稿尺寸保持原横向。|关闭打印对话框，修改幻灯片页面属性为纵向，再保存PPTX。|1\.0|0\.0|0\.0|0\.0|0\.0|0\.0|
|12|为 Impress 第一页添加 `dissolve` 切换效果。|将第二张幻灯片外观复制为第一页的近似副本，保留第一页内容及切换原值；预选第二页并打开切换面板，文本缩放2\.0。|核对页码，明确选中第一页后设置dissolve并保存。|1\.0|1\.0|1\.0|1\.0|0\.0|0\.0|
|13|将 Writer 文档标题居中对齐。|保持正文主标题处于未完成的左对齐状态，增加标明Running title的页脚；预选页脚并打开其段落对话框的Alignment页，文本缩放3\.0。 另将独立配置档编辑器置于前台，其Heading alignment显示Center，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，取消页脚段落对话框，选择正文第一段标题并居中，保存原文档。|1\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|14|将 Writer 全文字体改为 `Times New Roman`。|预选局部正文片段并打开样式编辑对话框；保留文本缩放2\.0，所有原正文文本与字体保持不变。|取消样式对话框，选择全文并统一设置Times New Roman，再保存。|1\.0|0\.0|1\.0|1\.0|0\.0|0\.0|
|15|在 Writer 当前页面之后插入空白页。|在原文档正文预选末尾位置并打开手动插入分隔符对话框；保留文本缩放2\.0，不预先增加分页符。|在对话框选择Page Break而非Line/Column Break，使显式分页符总数达到5。|0\.0|0\.0|1\.0|0\.0|1\.0|0\.0|
|16|在 Ubuntu 中通过命令行强制退出卡住的 Writer，不重启电脑、不影响其他工作。|增加只作用于现有Writer进程的图形Process Monitor窗口；End Process可以结束进程但不产生终端历史记录，原终端仍可访问。 另打开黑底Command Composer命令草稿窗口；输入命令及Enter仅编辑草稿，保存到writer\-maintenance\.sh，不执行命令、不写入真实终端历史；需要辨别草稿编辑与终端执行。|关闭配置档编辑器，切换到终端，用命令行结束Writer并确保命令记录持久化。|1\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|17|将 Ubuntu 默认视频播放器设置为 VLC。|默认视频关联保持原样，打开Default Applications后将真实VLC播放器置于前台；运行VLC不代表已经设置默认视频关联，文本缩放2\.0。 另将独立配置档编辑器置于前台，其Video player显示VLC，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，返回Default Applications，操作Video默认应用，选VLC。|0\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|18|从终端在 `指定的project目录` 文件夹中启动 VS Code。|增加名为project\-review的独立工作区并在VSCode前台打开，原目标project目录及终端保留；文本缩放2\.0。|返回终端执行code 指定的project目录，验证打开的是指定工作区。|1\.0|0\.0|1\.0|1\.0|1\.0|0\.0|
|19|在 Ubuntu 中启用勿扰模式。|注册并仅关闭Thunderbird应用级通知，保持全局show\-banners=true；打开Notifications应用级入口，文本缩放4\.0。 另将独立配置档编辑器置于前台，其Do Not Disturb显示True，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，使用GNOME日期/通知菜单开启全局勿扰，必要时先恢复文本缩放。|1\.0|0\.0|1\.0|1\.0|0\.0|0\.0|
|20|将系统输出音量调到最大。|保留系统输出音量原始20%，后台打开Sound的Input控件，再将VLC应用音量窗口置于前台；文本缩放4\.0。 另将独立配置档编辑器置于前台，其Default output volume \(%\)显示100，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，排除VLC和Input控件，切换至Output或系统菜单，将系统输出音量设置为100%。|1\.0|1\.0|1\.0|1\.0|0\.0|0\.0|
|21|将桌面目录 `todo_list_Jan_1` 重命名为 `todo_list_Jan_2`。|保留真实真实todo\_list\_Jan\_1目录目录，新增指向它的GTK书签并打开当前目录；预选侧栏书签的Rename入口，书签显示名的修改不改变真实目录名；文本缩放3\.0。|取消书签显示名编辑，返回Desktop父目录，重命名真正的todo\_list\_Jan\_1目录为todo\_list\_Jan\_2。|1\.0|1\.0|1\.0|1\.0|0\.0|0\.0|
|22|在 Thunderbird 创建 `COMPANY`、`UNIVERSITY` 两个本地文件夹。|在Outlook IMAP账户目录增加COMPANY和UNIVERSITY诱饵文件夹，并在文件管理器展示该账户目录；Thunderbird Local Folders保持原样，文本缩放2\.0。|回到Thunderbird，在Local Folders下创建两文件夹，而非接受账户目录中的同名文件夹。|1\.0|0\.0|1\.0|1\.0|0\.0|0\.0|
|23|在 Thunderbird 中启用完整深色模式。|将桌面配色设为深色但不修改Thunderbird activeThemeID，打开Thunderbird常规偏好设置；文本缩放2\.0。|进入Add\-ons and Themes并启用Thunderbird Dark主题。|1\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|24|为 Thunderbird 邮件账户设置纯文本签名：第一行 `Anonym`，第二行 `XYZ Lab`。|打开未发送的撰写窗口，正文含Anonym和XYZ Lab两行；不修改id1账户签名，保留文本缩放3\.0。|退出或忽略撰写窗口，在账户设置中填写id1纯文本签名。|1\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|25|启用 VLC 全屏模式，使视频填满整个屏幕。|保留当前循环播放，打开VLC Video首选项；文本缩放3\.0，首选项中的Fullscreen设置与当前播放状态形成范围差异。 另将独立配置档编辑器置于前台，其Full screen显示True，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，关闭首选项并让当前VLC视频窗口进入全屏。|1\.0|1\.0|1\.0|0\.0|1\.0|0\.0|
|26|将 VLC 可调最大音量从 125% 提高为原始音量的 200%。|打开VLC Audio首选项，保留文本缩放3\.0；原qt\-max\-volume仍为125，常规音量控件不是目标上限字段。 另将独立配置档编辑器置于前台，其Maximum volume \(%\)显示200，数据仅保存在独立配置档副本，不绑定实际活动应用；需要与活动应用/系统状态交叉核对。|关闭配置档编辑器，进入完整首选项的Qt界面设置，修改最大音量为200并保存。|1\.0|0\.0|0\.0|0\.0|0\.0|0\.0|
|27|将 VLC 录制内容保存目录改为 Desktop。|仅将VLC快照目录snapshot\-path设为Desktop，并打开首选项；录制目录input\-record\-path保持原值，文本缩放2\.0。需要区分快照路径与录制路径。|找到录制目录设置，将input\-record\-path设置为Desktop。|0\.0|0\.0|0\.0|0\.0|0\.0|0\.0|
|28|将 VS Code 颜色主题改为 `Visual Studio Dark`。|在工作区中的离线User设置副本创建离线配置副本并展示，但不注册为活动用户配置；实际User主题仍为Red，Code缩放8、文本缩放3\.0。|通过Open User Settings或颜色主题菜单修改活动User配置，不只编辑工作区中的离线副本。|0\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|29|将 VS Code 当前用户的代码换行列宽设置为 50 个字符。|工作区及离线User设置副本都含wordWrapColumn=50，实际User配置不预先设置目标；默认展示离线副本，Code缩放8、文本缩放3\.0。|缩小Code界面，通过Open User Settings打开真正活动的User配置，写入wordWrapColumn=50。|1\.0|0\.0|1\.0|0\.0|0\.0|0\.0|
|30|在 VS Code 启用自动保存，并将保存延迟设置为 500 毫秒。|工作区保留Python作用域500ms模板，并展示包含afterDelay/500的离线User设置副本；实际User目标不变，Code缩放8、文本缩放3\.0。|通过Open User Settings修改真正活动的User全局autoSave与autoSaveDelay，不只修改离线副本或语言模板。|1\.0|0\.0|1\.0|0\.0|0\.0|0\.0|



