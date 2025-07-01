document.addEventListener('DOMContentLoaded', () => {
    const navContainer = document.getElementById('journal-nav');
    const contentContainer = document.getElementById('content-container');
    const defaultJournal = 'AER';

    // 加载期刊数据
    async function loadJournalData(journal) {
        // 更新激活按钮样式
        document.querySelectorAll('.nav-button').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.journal === journal);
        });

        // 显示加载动画
        contentContainer.innerHTML = '<div class="loader">正在加载数据...</div>';

        try {
            const response = await fetch(`./${journal}.json?v=${new Date().getTime()}`); // 添加时间戳防止缓存
            if (!response.ok) {
                throw new Error(`无法加载 ${journal}.json 文件，状态码: ${response.status}`);
            }
            const data = await response.json();
            renderData(data);
        } catch (error) {
            console.error('获取数据失败:', error);
            contentContainer.innerHTML = `<div class="error-message">加载 ${journal} 数据失败。<br>可能是数据正在更新或首次生成中，请稍后再试。</div>`;
        }
    }

    // 渲染数据到页面
    function renderData(data) {
        if (data.error) {
             contentContainer.innerHTML = `<div class="error-message">抓取 ${data.journal_key} 数据时发生错误：<br>${data.error}</div>`;
             return;
        }

        let html = `
            <div id="journal-info">
                <h2>${data.journal_full_name}</h2>
                <p>更新时间: ${data.update_time}</p>
            </div>
        `;

        if (data.articles.length === 0) {
            html += '<p>当前没有找到新的文章。</p>';
        } else {
            data.articles.forEach((article, index) => {
                html += `
                    <div class="article-card">
                        <h3>${index + 1}. ${article.title}</h3>
                        <p class="title-cn">${article.title_cn}</p>
                        <p class="authors"><strong>Authors:</strong> ${article.authors}</p>
                        <a href="${article.url}" target="_blank" class="link">阅读原文 →</a>
                        
                        <details>
                            <summary>查看摘要 (Abstract)</summary>
                            <div class="abstract">
                                <h4>原文摘要</h4>
                                <p>${article.abstract.replace(/\n/g, '<br>')}</p>
                                <hr>
                                <h4>中文翻译</h4>
                                <p>${article.abstract_cn.replace(/\n/g, '<br>')}</p>
                            </div>
                        </details>
                    </div>
                `;
            });
        }
        contentContainer.innerHTML = html;
    }

    // 为导航按钮添加点击事件
    navContainer.addEventListener('click', (e) => {
        if (e.target.classList.contains('nav-button')) {
            const journal = e.target.dataset.journal;
            loadJournalData(journal);
        }
    });

    // 页面加载时，默认加载第一个期刊的数据
    loadJournalData(defaultJournal);
});
