document.addEventListener('DOMContentLoaded', () => {
    const navContainer = document.getElementById('journal-nav');
    const contentContainer = document.getElementById('content-container');
    const defaultJournal = 'AER';

    async function loadJournalData(journal) {
        document.querySelectorAll('.nav-button').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.journal === journal);
        });

        contentContainer.innerHTML = '<div class="loader">正在加载数据...</div>';

        try {
            const response = await fetch(`./${journal}.json?v=${new Date().getTime()}`);
            if (!response.ok) throw new Error(`无法加载 ${journal}.json`);
            const data = await response.json();
            renderData(data);
        } catch (error) {
            console.error('获取数据失败:', error);
            contentContainer.innerHTML = `<div class="error-message">加载 ${journal} 数据失败。<br>请稍后再试。</div>`;
        }
    }

    function renderData(data) {
        if (data.error) {
             contentContainer.innerHTML = `<div class="error-message">抓取 ${data.journal_key} 数据时发生错误：<br>${data.error}</div>`;
             return;
        }

        let html = `
            <div id="journal-info">
                <h2>${data.journal_full_name}</h2>
                <span class="issue-title">${data.report_header}</span>
                <span class="update-time">数据更新于: ${data.update_time}</span>
            </div>
        `;

        if (data.articles.length === 0) {
            html += '<p style="text-align:center;">当前没有找到新的文章。</p>';
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
                            <div class="abstract-content">
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

    navContainer.addEventListener('click', (e) => {
        if (e.target.classList.contains('nav-button')) {
            loadJournalData(e.target.dataset.journal);
        }
    });

    loadJournalData(defaultJournal);
});
