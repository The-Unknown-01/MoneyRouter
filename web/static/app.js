document.addEventListener('DOMContentLoaded', () => {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const overlay = document.getElementById('work-overlay');
  const overlayLabel = document.getElementById('work-overlay-label');
  const pendingLabels = {
    '/register': '正在创建账号…',
    '/login': '正在登录…',
    '/logout': '正在退出…',
    '/profile/ask': '助手正在理解你的情况…',
    '/profile': '正在确认三项摘要…',
    '/ledger': '正在保存账单…',
    '/ledger/quick': '正在汇总分类开销…',
    '/ledger/skip': '正在准备临时方案…',
    '/ledger/delete': '正在删除记录…',
    '/ledger/import': '正在解析 CSV…',
    '/ledger/preview': '正在更新导入预览…',
    '/ledger/confirm': '正在导入账单…',
    '/demo': '正在准备模拟账本…',
    '/plan/generate': '正在计算并生成方案…',
    '/plan/adjust': '正在重新计算方案…',
    '/chat': '方案助手正在回答…',
    '/clear': '正在清空账号数据…'
  };
  const navigateWithMotion = destination => {
    if (document.body.classList.contains('is-leaving')) return;
    document.body.classList.add('is-leaving');
    window.setTimeout(() => location.assign(destination), reducedMotion.matches ? 0 : 170);
  };

  const form = document.getElementById('transaction-form');
  if (form) {
    const fields = { id:'tx-id', date:'tx-date', direction:'tx-direction', amount:'tx-amount', category:'tx-category', description:'tx-description', note:'tx-note' };
    document.querySelectorAll('.edit-row').forEach(button => button.addEventListener('click', () => {
      const manualEntry = document.querySelector('.entry-choice');
      if (manualEntry) manualEntry.open = true;
      for (const [key, element] of Object.entries(fields)) document.getElementById(element).value = button.dataset[key] || '';
      document.getElementById('tx-submit').textContent = '保存修改 →';
      form.classList.remove('form-highlight');
      void form.offsetWidth;
      form.classList.add('form-highlight');
      form.scrollIntoView({behavior: reducedMotion.matches ? 'auto' : 'smooth', block:'start'});
    }));
    document.getElementById('tx-reset')?.addEventListener('click', () => {
      form.reset(); document.getElementById('tx-id').value='';
      document.getElementById('tx-date').value=new Date().toLocaleDateString('sv-SE');
      document.getElementById('tx-submit').textContent='保存记录 →';
    });
  }
  document.querySelectorAll('.confirm-delete').forEach(form => form.addEventListener('submit', event => { if (!confirm('确定删除这笔记录？')) event.preventDefault(); }));
  document.querySelectorAll('.confirm-clear').forEach(form => form.addEventListener('submit', event => { if (!confirm('确定清空当前账号的账本、画像、方案和对话？')) event.preventDefault(); }));

  document.querySelectorAll('.dropzone input[type="file"]').forEach(input => input.addEventListener('change', () => {
    const label = input.closest('.dropzone');
    const title = label?.querySelector('strong');
    if (!title) return;
    title.textContent = input.files?.[0]?.name || '选择 CSV 文件';
    label.classList.toggle('file-picked', Boolean(input.files?.length));
  }));

  document.querySelectorAll('form[method="post"]').forEach(postForm => postForm.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    if (postForm.dataset.submitting === 'true') {
      event.preventDefault();
      return;
    }
    postForm.dataset.submitting = 'true';
    postForm.setAttribute('aria-busy', 'true');
    postForm.querySelector('button[type="submit"], button:not([type])')?.classList.add('is-busy');
    const path = new URL(postForm.action, location.href).pathname;
    if (overlayLabel) overlayLabel.textContent = pendingLabels[path] || '正在处理…';
    window.setTimeout(() => {
      if (overlay) {
        overlay.setAttribute('aria-hidden', 'false');
        overlay.classList.add('visible');
      }
    }, reducedMotion.matches ? 0 : 140);
  }));

  document.querySelectorAll('form[method="get"]').forEach(getForm => getForm.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    event.preventDefault();
    const destination = new URL(getForm.action, location.href);
    destination.search = new URLSearchParams(new FormData(getForm)).toString();
    navigateWithMotion(destination.href);
  }));

  document.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target instanceof Element ? event.target.closest('a[href]') : null;
    if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
    const destination = new URL(link.href, location.href);
    if (destination.origin !== location.origin || destination.pathname.startsWith('/static/')) return;
    if (destination.pathname === location.pathname && destination.search === location.search && destination.hash) return;
    event.preventDefault();
    navigateWithMotion(destination.href);
  });

  window.addEventListener('pageshow', () => {
    document.body.classList.remove('is-leaving');
    overlay?.classList.remove('visible');
    overlay?.setAttribute('aria-hidden', 'true');
    document.querySelectorAll('form[data-submitting]').forEach(postForm => {
      delete postForm.dataset.submitting;
      postForm.removeAttribute('aria-busy');
      postForm.querySelector('.is-busy')?.classList.remove('is-busy');
    });
  });
});
