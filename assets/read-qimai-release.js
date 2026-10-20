(() => {
  // Read only rendered application-header evidence, never page state or private APIs.
  const header = document.querySelector('.app-header');
  const shown = el => el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const item = [...(header?.querySelectorAll('.new-item') || [])]
    .find(el => shown(el) && el.querySelector('.type')?.textContent.trim() === '最早发布');
  const sourceLabel = item?.querySelector('.type')?.textContent.trim() || '';
  const rawText = item?.innerText.trim() || '';
  const value = item?.querySelector('.value')?.textContent.trim() || '';
  const selectedFlag = [...(header?.querySelectorAll('.ivu-dropdown-rel .icon-flag') || [])].find(shown);
  const pageCountry = selectedFlag?.parentElement.querySelector('span')?.innerText.trim() || '';
  const pageAppId = header?.querySelector('.i-id .new-value')?.textContent.trim() || '';
  const route = new URL(document.URL).pathname.match(/^\/app\/(?:rank|baseinfo)\/appid\/(\d+)\/country\/([a-z]{2})\/?$/);
  const ready = !!pageAppId && !!pageCountry && !!route;
  const verified = ready && /^\d{4}-\d{2}-\d{2}$/.test(value);
  return {
    sourceUrl: document.URL, title: document.title, appId: route?.[1] || '', country: route?.[2] || '',
    pageAppId, pageCountry, sourceLabel, rawText,
    releaseDate: verified ? value : null, status: verified ? 'verified' : ready ? 'unavailable' : 'failed',
    ...(!verified ? { reason: ready ? '详情页未显示可识别的最早发布日期' : '详情页App ID或国家尚未完整加载' } : {})
  };
})()
