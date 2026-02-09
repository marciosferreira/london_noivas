const statusMap = {
  rented: 'Alugado',
  returned: 'Devolvido',
  historic: 'Histórico',
  available: 'Inventário',
  deleted: 'Deletado',
  archive: 'Arquivado',
};

function definirMensagemEConfirmar(itemData, mensagem, form) {
  if (itemData.status !== 'deleted') {
    alert('Status inválido para restauração.');
    return;
  }

  form.action = '/restore_deleted_item';

  if (confirm(mensagem)) {
    document.getElementById('restore_item_data').value = JSON.stringify(itemData);
    form.submit();
  }
}

function restaurarItem(itemData) {
  console.log('Chamou a função restaurarItem');
  console.log(itemData);
  let form = document.getElementById('form-restore');

  let previousStatusTranslated = statusMap[itemData.previous_status] || 'Indefinido';
  definirMensagemEConfirmar(
    itemData,
    `Restaurar este item para o status anterior: ${previousStatusTranslated}?`,
    form,
  );
}

window.restaurarItem = restaurarItem;
