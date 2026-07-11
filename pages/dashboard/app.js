const bridge = window.AstrBotPluginPage;
let newsPage = 0;
let taskPollTimer = null;
let selectedTargetUmos = new Set();

/* ====== 工具函数 ====== */

function toast(msg) {
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 3000);
}

function setButtonsDisabled(disabled) {
    ['btn-collect', 'btn-daily'].forEach(id => {
        const btn = document.getElementById(id);
        if (btn) btn.disabled = disabled;
    });
}

function showTaskBar(message, progress) {
    const bar = document.getElementById('task-bar');
    if (!bar) return;
    bar.className = 'task-bar active';
    document.getElementById('task-message').textContent = message;
    document.getElementById('task-progress').style.width = progress + '%';
}

function hideTaskBar() {
    const bar = document.getElementById('task-bar');
    if (bar) bar.className = 'task-bar';
}

function completeTaskBar(message) {
    const bar = document.getElementById('task-bar');
    if (!bar) return;
    bar.className = 'task-bar active done';
    document.getElementById('task-message').textContent = message;
    document.getElementById('task-progress').style.width = '100%';
    setTimeout(hideTaskBar, 5000);
}

function failTaskBar(message) {
    const bar = document.getElementById('task-bar');
    if (!bar) return;
    bar.className = 'task-bar active failed';
    document.getElementById('task-message').textContent = message;
    setTimeout(hideTaskBar, 8000);
}

async function pollTask(taskId) {
    if (taskPollTimer) clearInterval(taskPollTimer);
    setButtonsDisabled(true);

    taskPollTimer = setInterval(async () => {
        try {
            const task = await bridge.apiGet("task/" + taskId);
            showTaskBar(task.message, task.progress);

            if (task.status === 'completed') {
                clearInterval(taskPollTimer);
                taskPollTimer = null;
                setButtonsDisabled(false);
                completeTaskBar(task.message);
                loadDashboard();
            } else if (task.status === 'failed') {
                clearInterval(taskPollTimer);
                taskPollTimer = null;
                setButtonsDisabled(false);
                failTaskBar(task.message);
            }
        } catch (e) {
            clearInterval(taskPollTimer);
            taskPollTimer = null;
            setButtonsDisabled(false);
            failTaskBar('任务状态丢失，服务可能已重启');
        }
    }, 1500);
}

async function startTask(endpoint, taskName) {
    showTaskBar(taskName + ' 任务启动中...', 5);
    try {
        const r = await bridge.apiPost(endpoint);
        if (r.task_id) {
            pollTask(r.task_id);
        } else {
            toast(r.message || '启动失败');
            hideTaskBar();
        }
    } catch (e) {
        toast('请求失败: ' + e.message);
        hideTaskBar();
    }
}

/* ====== Tab 切换 ====== */

window.switchTab = function (name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
    const loaders = {
        dashboard: loadDashboard, news: loadNews,
        newsletters: loadNewsletters, push: loadPushTargets, system: loadSystem,
    };
    if (loaders[name]) loaders[name]();
};

/* ====== 渲染组件 ====== */

window.renderNewsItem = function (n) {
    const translated = n.translated
        ? '<span class="badge badge-translated">已翻译</span>' : '';
    const originalTitle = n.title_original
        ? '<div class="original">原文: ' + escapeHtml(n.title_original) + '</div>' : '';
    const summary = (n.summary || '').substring(0, 180);
    return '<div class="news-item">'
        + '<h3><a href="' + escapeHtml(n.link) + '" target="_blank">' + escapeHtml(n.title) + '</a></h3>'
        + originalTitle
        + '<div class="meta">'
        + '<span class="badge badge-source">' + escapeHtml(n.source) + '</span>'
        + '<span class="badge badge-category">' + escapeHtml(n.category || '未分类') + '</span>'
        + translated
        + '<span>' + escapeHtml(n.published || '') + '</span>'
        + '</div>'
        + '<p class="summary">' + escapeHtml(summary) + (summary.length >= 180 ? '...' : '') + '</p>'
        + '</div>';
};

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

/* ====== 概览面板 ====== */

async function loadDashboard() {
    try {
        const s = await bridge.apiGet("status");
        const statsEl = document.getElementById('stats');
        if (statsEl) {
            statsEl.innerHTML =
                '<div class="stat"><div class="label">新闻总数</div><div class="value">' + s.news_count + '</div></div>'
                + '<div class="stat"><div class="label">已翻译</div><div class="value">' + (s.translated_count || 0) + '</div></div>'
                + '<div class="stat"><div class="label">AI 状态</div><div class="value small">' + (s.ai_configured ? '✅' : '❌') + '</div></div>'
                + '<div class="stat"><div class="label">定时任务</div><div class="value small">' + (s.schedule_time || '--') + '</div></div>';
        }

        const catStats = s.category_stats || {};
        const catOrder = ['中文', '财经', '科技', '英文', '未分类'];
        let catHtml = '';
        for (const cat of [...catOrder, ...Object.keys(catStats).filter(c => !catOrder.includes(c))]) {
            if (catStats[cat]) {
                catHtml += '<span class="badge badge-category" style="font-size:13px;padding:5px 10px">'
                    + cat + ': ' + catStats[cat] + '条</span>';
            }
        }
        const catOverview = document.getElementById('category-overview');
        if (catOverview) catOverview.innerHTML = catHtml || '<span class="empty">暂无数据</span>';

        const newsData = await bridge.apiGet("news", { limit: 5 });
        const news = newsData.news || [];
        const recentNews = document.getElementById('recent-news');
        if (recentNews) {
            recentNews.innerHTML = news.length
                ? news.map(n => window.renderNewsItem(n)).join('')
                : '<div class="empty">暂无新闻</div>';
        }
    } catch (e) {
        console.error('loadDashboard error:', e);
    }
}

/* ====== 新闻流面板 ====== */

window.loadNews = async function () {
    const category = document.getElementById('news-category-filter').value;
    const source = document.getElementById('news-source-filter').value;
    const limit = parseInt(document.getElementById('news-limit').value) || 50;
    const offset = newsPage * limit;

    let params = { limit: limit, offset: offset };
    if (category) params.category = category;
    if (source) params.source = source;

    try {
        const data = await bridge.apiGet("news", params);
        const news = data.news || [];
        const total = data.total || news.length;

        const grouped = {};
        const catOrder = ['中文', '财经', '科技', '英文', '未分类'];
        for (const n of news) {
            const cat = n.category || '未分类';
            if (!grouped[cat]) grouped[cat] = [];
            grouped[cat].push(n);
        }

        let html = '<p style="margin-bottom:10px;color:var(--text-secondary);font-size:12px">'
            + '共 ' + total + ' 条新闻 | 第 ' + (offset + 1) + '-' + Math.min(offset + limit, total) + ' 条</p>';

        for (const cat of [...catOrder, ...Object.keys(grouped).filter(c => !catOrder.includes(c))]) {
            if (!grouped[cat]) continue;
            html += '<div class="category-section">'
                + '<div class="card-header">' + cat + ' <span class="count" style="font-size:12px;color:var(--text-secondary);font-weight:normal">' + grouped[cat].length + '条</span></div>'
                + grouped[cat].map(n => window.renderNewsItem(n)).join('')
                + '</div>';
        }

        const newsList = document.getElementById('news-list');
        if (newsList) newsList.innerHTML = html || '<div class="empty">暂无新闻</div>';

        const totalPages = Math.ceil(total / limit) || 1;
        let pagHtml = '';
        if (newsPage > 0) pagHtml += '<button class="btn btn-primary btn-sm" onclick="newsPage--;loadNews()">上一页</button>';
        pagHtml += '<span style="font-size:12px;color:var(--text-secondary);line-height:28px">第 ' + (newsPage + 1) + '/' + totalPages + ' 页</span>';
        if (newsPage < totalPages - 1) pagHtml += '<button class="btn btn-primary btn-sm" onclick="newsPage++;loadNews()">下一页</button>';
        const pagEl = document.getElementById('news-pagination');
        if (pagEl) pagEl.innerHTML = pagHtml;
    } catch (e) {
        console.error('loadNews error:', e);
    }
};

/* ====== 简报面板 ====== */

async function loadNewsletters() {
    try {
        const list = await bridge.apiGet("newsletters");
        const el = document.getElementById('newsletter-list');
        if (!el) return;
        if (!list || !list.length) {
            el.innerHTML = '<div class="empty">暂无简报</div>';
            return;
        }
        el.innerHTML = list.map(n => {
            let genTime = n.generated_at || '';
            try {
                const d = new Date(genTime);
                if (!isNaN(d.getTime())) {
                    genTime = d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
                }
            } catch (e) {}
            return '<div class="newsletter-item">'
                + '<div class="info"><h3>' + escapeHtml(n.title) + '</h3>'
                + '<p>' + n.date + ' | ' + genTime + '</p></div>'
                + '<a href="javascript:void(0)" class="btn btn-primary btn-sm" onclick="previewNewsletter(\''
                + n.date + '\')">查看</a>'
                + '</div>';
        }).join('');
    } catch (e) {
        console.error('loadNewsletters error:', e);
    }
}

window.generateNewsletter = async function() {
    await startTask('generate-newsletter', '简报生成');
};

window.previewNewsletter = async function(date) {
    try {
        const data = await bridge.apiGet('newsletters/' + date);
        if (data && data.content) {
            document.getElementById('newsletter-preview-title').textContent = data.title || date;
            document.getElementById('newsletter-preview-frame').srcdoc = data.content;
            document.getElementById('newsletter-preview').hidden = false;
        } else {
            toast('该简报没有可预览内容');
        }
    } catch (e) {
        toast('获取简报失败: ' + e.message);
    }
};

window.closeNewsletterPreview = function() {
    document.getElementById('newsletter-preview-frame').srcdoc = '';
    document.getElementById('newsletter-preview').hidden = true;
};

/* ====== 推送面板 ====== */

window.loadPushTargets = async function() {
    const select = document.getElementById('target-sessions-select');
    const status = document.getElementById('target-sessions-status');
    if (!select || !status) return;

    status.textContent = '正在加载会话…';
    try {
        const data = await bridge.apiGet('target-sessions');
        const targets = data.targets || [];
        selectedTargetUmos = new Set(targets.map(target => target.unified_msg_origin).filter(Boolean));
        const umos = data.umos || [];
        select.innerHTML = umos.length
            ? umos.map(umo => '<option value="' + escapeHtml(umo) + '"'
                + (selectedTargetUmos.has(umo) ? ' selected' : '') + '>'
                + escapeHtml(umo) + '</option>').join('')
            : '<option disabled>暂无可选会话，请先在目标群或私聊中发送一条消息</option>';
        status.textContent = selectedTargetUmos.size
            ? '已选择 ' + selectedTargetUmos.size + ' 个推送目标'
            : '尚未选择推送目标';
    } catch (e) {
        status.textContent = '加载失败';
        toast('获取可选会话失败: ' + e.message);
    }
};

window.savePushTargets = async function() {
    const select = document.getElementById('target-sessions-select');
    const status = document.getElementById('target-sessions-status');
    if (!select || !status) return;

    const sessions = Array.from(select.selectedOptions).map(option => ({
        note: option.textContent,
        unified_msg_origin: option.value,
    }));
    status.textContent = '正在保存…';
    try {
        const data = await bridge.apiPost('target-sessions', { sessions });
        selectedTargetUmos = new Set((data.targets || []).map(target => target.unified_msg_origin));
        status.textContent = selectedTargetUmos.size
            ? '已保存 ' + selectedTargetUmos.size + ' 个推送目标'
            : '已清空推送目标';
        toast('推送目标已保存');
    } catch (e) {
        status.textContent = '保存失败';
        toast('保存推送目标失败: ' + e.message);
    }
};

/* ====== 系统面板 ====== */

async function loadSystem() {
    try {
        const s = await bridge.apiGet("status");
        const sysInfo = document.getElementById('system-info-container');
        // system info is rendered inline via stats
    } catch (e) {
        console.error('loadSystem error:', e);
    }
}

/* ====== 操作按钮 ====== */

window.collectNews = async function() {
    await startTask('collect', '采集');
};

window.runDaily = async function() {
    await startTask('run-daily', '每日任务');
};

/* ====== 初始化 ====== */

async function initFilters() {
    try {
        const catData = await bridge.apiGet("categories");
        const catSelect = document.getElementById('news-category-filter');
        if (catSelect && catData) {
            const cats = catData.categories || {};
            const order = catData.order || [];
            for (const cat of order) {
                if (cats[cat] && cats[cat] > 0) {
                    catSelect.innerHTML += '<option value="' + cat + '">' + cat + ' (' + cats[cat] + '条)</option>';
                }
            }
        }

        const s = await bridge.apiGet("status");
        const srcSelect = document.getElementById('news-source-filter');
        if (srcSelect && s.source_stats) {
            for (const src of Object.keys(s.source_stats)) {
                srcSelect.innerHTML += '<option value="' + src + '">' + src + '</option>';
            }
        }
    } catch (e) {
        console.error('initFilters error:', e);
    }
}

(async () => {
    try {
        const ctx = await bridge.ready();
        console.log('NewsFlow Plugin Page 已就绪:', ctx.pluginName, ctx.pageName);
        await initFilters();
        await loadDashboard();
        bridge.onContext(() => { loadDashboard(); });
    } catch (e) {
        console.error('Plugin Page 初始化失败:', e);
    }
})();
