const statusMap = {
  rented: 'Alugado',
  returned: 'Devolvido',
  historic: 'Histórico',
  available: 'Inventário',
  deleted: 'Deletado',
  version: 'Versão',
  archive: 'Arquivado',
};

function definirMensagemEConfirmar(itemData, parentData, mensagem, form) {
  if (!['deleted', 'version'].includes(itemData.status)) {
    alert('Status inválido para restauração.');
    return;
  }

  form.action = itemData.status === 'deleted' ? '/restore_deleted_item' : '/restore_version_item';

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

  if (itemData.status !== 'version') {
    definirMensagemEConfirmar(itemData, {}, `Restaurar este item pai para o status anterior: ${previousStatusTranslated}?`, form);
    return;
  }

  fetch('/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ db_name: 'itens_table', key: 'item_id', value: itemData.parent_item_id, type: 'item' }),
  })
    .then((response) => response.json())
    .then((parentData) => {
      if (parentData.status === 'not_found' || !parentData.data) {
        alert('Item pai não encontrado.');
        return;
      }

      let parentStatusTranslated = statusMap[parentData.data.status] || 'Indefinido';

      let mensagem =
        parentData.data.status === 'deleted'
          ? `Esta é uma versão de um item pai deletado. Restaurar esta versão para o status: ${previousStatusTranslated}?`
          : `Há um item pai desta versão com o status: ${parentStatusTranslated}. Deseja substituir o item pai com esta versão?`;

      definirMensagemEConfirmar(itemData, parentData.data, mensagem, form);
    })
    .catch((error) => {
      alert('Erro ao consultar o banco de dados.');
      console.error(error);
    });
}

window.restaurarItem = restaurarItem;
