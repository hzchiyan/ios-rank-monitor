(() => {
  // Only execute through the browser's documented read-only DOM API.
  const txt = (parent, selector) => parent.querySelector(selector)?.textContent.trim() || '';
  const rows = [...document.querySelectorAll('tbody tr')].filter(tr => tr.querySelector('a.app-name'));
  return {
    title: document.title,
    sourceDate: document.querySelector('#date-box')?.textContent.trim() || '',
    loadedRowCount: rows.length,
    completeTop80: rows.length >= 80,
    rows: rows.slice(0, 80).map(tr => ({
      position: +txt(tr, 'td .index'), name: txt(tr, '.app-name'),
      qimaiUrl: 'https://www.qimai.cn' + tr.querySelector('.app-name').getAttribute('href'),
      developer: txt(tr, '.company'), rating: txt(tr, '.comment-rating'),
      ratingCount: txt(tr, '.comment-num'), latestVersionDate: txt(tr, '.app-version'),
      ranks: [...tr.querySelectorAll('.rank-wrap')].map(r => ({rank: txt(r, '.num'), category: txt(r, '.category')}))
    }))
  };
})()
