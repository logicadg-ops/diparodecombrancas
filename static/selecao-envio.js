// Seleção de unidades para o disparo em massa: o envio em massa só vai para
// as linhas marcadas. "Selecionar todos" marca as linhas exibidas (respeita
// os filtros); "Desmarcar todos" limpa tudo.
function iniciarSelecaoEnvio(linhas) {
  const resumo = document.getElementById('resumo-selecao');
  const caixa = (tr) => tr.querySelector('.chk-envio');

  function atualizar() {
    const marcadas = linhas.filter((tr) => caixa(tr).checked).length;
    const ocultas = linhas.filter((tr) => tr.hidden && caixa(tr).checked).length;
    resumo.textContent = `${marcadas} unidade(s) selecionada(s) para o envio em massa` +
      (ocultas ? ` (${ocultas} fora do filtro atual)` : '');
  }

  document.getElementById('btn-selecionar-todos').addEventListener('click', () => {
    linhas.forEach((tr) => { if (!tr.hidden) caixa(tr).checked = true; });
    atualizar();
  });
  document.getElementById('btn-desmarcar-todos').addEventListener('click', () => {
    linhas.forEach((tr) => { caixa(tr).checked = false; });
    atualizar();
  });
  linhas.forEach((tr) => caixa(tr).addEventListener('change', atualizar));
  atualizar();

  return {
    atualizar,
    // Formulários de envio das linhas marcadas, na ordem da tabela.
    selecionados: () => linhas.filter((tr) => caixa(tr).checked)
      .map((tr) => tr.querySelector('.form-enviar-unidade')),
    // Depois de enviar com sucesso, desmarca pra não reenviar no próximo clique.
    desmarcar: (form) => { caixa(form.closest('tr')).checked = false; atualizar(); },
  };
}
