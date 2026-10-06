document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('transaction-form');
  if (form) {
    const fields = { id:'tx-id', date:'tx-date', direction:'tx-direction', amount:'tx-amount', category:'tx-category', description:'tx-description', note:'tx-note' };
    document.querySelectorAll('.edit-row').forEach(button => button.addEventListener('click', () => {
      const manualEntry = document.querySelector('.entry-choice');
      if (manualEntry) manualEntry.open = true;
      for (const [key, element] of Object.entries(fields)) document.getElementById(element).value = button.dataset[key] || '';
      document.getElementById('tx-submit').textContent = '保存修改 →';
      form.scrollIntoView({behavior:'smooth',block:'start'});
    }));
    document.getElementById('tx-reset')?.addEventListener('click', () => {
      form.reset(); document.getElementById('tx-id').value='';
      document.getElementById('tx-date').value=new Date().toLocaleDateString('sv-SE');
      document.getElementById('tx-submit').textContent='保存记录 →';
    });
  }
  document.querySelectorAll('.confirm-delete').forEach(form => form.addEventListener('submit', event => { if (!confirm('确定删除这笔记录？')) event.preventDefault(); }));
  document.querySelectorAll('.confirm-clear').forEach(form => form.addEventListener('submit', event => { if (!confirm('确定清空当前账号的账本、画像、方案和对话？')) event.preventDefault(); }));
});
