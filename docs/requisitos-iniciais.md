# Prompt mestre: plataforma de transcrição sob demanda

Você é a IA responsável por **projetar, implementar, testar e preparar para produção** uma aplicação web de transcrição de áudio. Este documento é a especificação de produto e o roteiro de execução. Trabalhe em etapas pequenas, entregue código executável e registre as decisões técnicas. Não substitua requisitos por uma demonstração superficial.

O produto atende inicialmente clientes no Brasil, em português brasileiro, com preços em reais. Seu diferencial é uma cobrança justa **por arquivo transcrito**, próxima do custo operacional. **Não implemente assinaturas, planos mensais ou anuais, compra de créditos, saldo de usuário ou cobrança recorrente.**

## 1. Resultado esperado

Um usuário autenticado deve conseguir enviar um áudio, conhecer e aceitar um preço final antes de pagar, pagar uma única vez via Pix ou cartão, acompanhar o processamento e receber uma transcrição com texto, marcações de tempo e indicação de quem falou em cada trecho. O sistema deve aceitar gravações de até **2 horas**, inclusive um caso real de **1h20**, e distinguir até **quatro falantes** de forma consistente ao longo do arquivo. Exiba `Falante 1`, `Falante 2` etc. até que o usuário altere os nomes. Não tente inferir a identidade civil das pessoas apenas pela fala.

O usuário deve poder ouvir o áudio enquanto revisa o texto, corrigir a transcrição e os nomes dos falantes e exportar o resultado em TXT e SRT. O TXT deve incluir tempos e falantes; o SRT deve preservar tempos válidos e incluir o falante no texto da legenda. A edição deve ser salva sem alterar o resultado bruto, para permitir auditoria e recuperação.

Para manter o MVP enxuto, não inclua transcrição ao vivo, tradução, aplicativo móvel nativo, editor colaborativo, resumo por IA, API pública para terceiros nem múltiplos gateways. Estruture o código para permitir troca futura do modelo de transcrição ou gateway sem reescrever o fluxo central.

## 2. Arquitetura e implantação

Use **Django** para backend, autenticação, páginas e painel administrativo; templates do Django com JavaScript pontual para upload, acompanhamento e edição; **PostgreSQL** para dados; **Celery + Redis** para filas; armazenamento privado **compatível com S3** para áudio e exportações; **FFmpeg/ffprobe** para inspeção e preparação do áudio; API de transcrição da **OpenAI**; **Asaas** para pagamentos. Use Docker Compose para desenvolvimento e documente uma implantação de produção reproduzível com TLS, processos web e worker separados, segredos por variáveis de ambiente, migrações controladas, backup e restauração do banco. Não exponha PostgreSQL, Redis ou storage diretamente à internet.

Prefira um monólito Django bem organizado. Separe os módulos de contas, arquivos/transcrições, precificação e pagamentos. Defina interfaces internas pequenas para `TranscriptionProvider` e `PaymentGateway`; implemente somente OpenAI e Asaas no MVP. Não armazene credenciais, dados completos de cartão nem áudio no banco. Use o checkout/fatura hospedada pelo Asaas para cartão, evitando coleta direta de dados de pagamento.

Antes de integrar serviços externos, consulte a documentação oficial **atual** e registre no README a versão ou data consultada. O modelo inicial é `gpt-4o-transcribe-diarize`, configurável por ambiente ou administração, com `response_format=diarized_json` e estratégia de chunking apropriada. Verifique se o modelo ainda está disponível, seus limites de duração/tamanho/saída e preços vigentes. Se houver descontinuação ou substituição, apresente a alteração necessária e teste a equivalência funcional antes de trocar. Referências iniciais: [transcrição de arquivos](https://developers.openai.com/api/docs/guides/speech-to-text), [modelo com diarização](https://developers.openai.com/api/docs/models/gpt-4o-transcribe-diarize) e [preços da API](https://developers.openai.com/api/docs/pricing).

## 3. Fluxo do usuário e dados

1. Cadastro/login por e-mail e senha, com confirmação de e-mail e redefinição de senha. Cada usuário vê somente seus pedidos, áudios, textos e pagamentos. O administrador usa permissões próprias do Django.
2. Upload privado com barra de progresso. Aceite formatos de áudio que a cadeia FFmpeg/OpenAI consiga decodificar e validar; rejeite conteúdo inválido, duração acima de 2 horas e arquivo acima de um limite inicial configurável de 100 MB. Faça upload direto para storage com URL temporária, ou streaming pelo servidor se o ambiente escolhido suportar o limite sem consumir memória excessiva. Valide no servidor extensão, assinatura do arquivo, duração e decodificação; não confie nos metadados enviados pelo navegador.
3. Após validar o áudio, produza uma cotação em BRL com duração, preço total, método de pagamento, itens de custo estimados e prazo de validade de 30 minutos. Mostre o preço final antes de criar a cobrança. Congele os parâmetros utilizados na cotação; alterações posteriores na tabela não mudam pedidos já cotados.
4. Crie uma **cobrança avulsa** no Asaas vinculada a um pedido local único. Ofereça Pix e cartão, conforme os métodos realmente habilitados na conta. Não crie assinatura. Um retorno do navegador da página de pagamento não prova pagamento: só o estado validado pelo servidor autoriza o processamento.
5. Após confirmação de pagamento, enfileire a transcrição uma única vez. Mostre estados claros: aguardando pagamento, pago/na fila, processando, pronto, falhou, estorno pendente, estornado. Não mostre transcrição parcial como entrega final.
6. Na página do resultado, sincronize reprodução e segmentos por tempo; permita editar texto, atribuir/renomear falantes e baixar TXT/SRT. Guarde versão bruta e versão revisada. Um pedido pronto continua acessível após a exclusão do áudio, mas a reprodução deixa de estar disponível.

Modele ao menos `User`, `AudioJob`, `Quote`, `Payment`, `WebhookEvent`, `Speaker`, `TranscriptSegment`, `UsageRecord` e `PricingConfig`, com IDs únicos, timestamps e relações explícitas. Use `Decimal` ou centavos inteiros para dinheiro, nunca `float`. Defina estados e transições permitidas para pedido e pagamento; atualizações críticas devem ser transacionais e idempotentes.

## 4. Precificação por demanda

O valor é **fechado e pago antes do processamento**. Como os tokens reais ainda não são conhecidos, estime os tokens a partir da duração e de coeficientes calibráveis separados para entrada de áudio e saída de texto. Use as tarifas atuais do modelo em USD, uma cotação USD/BRL configurável e registrada na cotação, custo estimado de infraestrutura por minuto, tarifa fixa e percentual do meio de pagamento e margem inicial configurável de **10%** sobre API + infraestrutura. A tarifa do gateway deve ser incorporada ao preço exibido, não acrescentada após o aceite.

Implemente a fórmula de modo auditável, com arredondamento para cima ao centavo:

`base = custo_api_estimado_brl + custo_infra_estimado_brl`

`preco_total = arredondar_para_cima((base × (1 + margem) + tarifa_fixa_gateway) ÷ (1 - tarifa_percentual_gateway))`

Se houver outras tarifas obrigatórias do meio de pagamento, inclua-as na configuração e na fórmula documentada. Os coeficientes de tokens/minuto, tarifas, câmbio, custo de infraestrutura e margem devem ser editáveis somente por administrador, com histórico de alterações. **Bloqueie cobranças reais** enquanto qualquer parâmetro obrigatório estiver ausente, desatualizado ou não validado pelo administrador. Não invente uma tarifa permanente a partir dos números deste prompt; consulte os preços oficiais e as condições reais da conta Asaas na implementação.

Após cada trabalho, grave uso de tokens reportado pelo provedor, valor real estimado em BRL, taxas efetivas do gateway, preço cobrado e diferença para o custo. Use agregados para ajustar os coeficientes futuros. **Não cobre diferença nem devolva automaticamente uma diferença pequena** entre estimativa e custo real de um pedido concluído: o preço aceito é final. Exponha ao administrador alertas de margem negativa e discrepâncias sistemáticas. O cliente vê o preço final e o fato de que ele foi calculado a partir de custos estimados, sem promessa de equivalência exata aos tokens finais.

## 5. Transcrição longa e falantes

Prepare o áudio em mono apropriado para fala, preservando inteligibilidade e sem sobrescrever o original. Processe em trechos curtos o suficiente para respeitar **todos** os limites atuais da API, inclusive tamanho de arquivo e saída do modelo; comece com trechos de aproximadamente 4 minutos e pequena sobreposição, ajustando por testes reais. A documentação inicial informa limite de **25 MB por arquivo** e exige `diarized_json` para receber segmentos com falante, início e fim. Para áudio acima de 30 segundos, configure `chunking_strategy` conforme a documentação. Nunca corte uma palavra deliberadamente quando puder deslocar o limite para uma pausa.

Converta tempos locais de cada trecho em tempos absolutos do áudio. Remova duplicações da sobreposição antes de compor a transcrição final. Um mesmo falante deve manter o mesmo número entre trechos: extraia amostras limpas de 2 a 10 segundos quando houver fala suficiente, mantenha até quatro referências de voz e utilize `known_speaker_names`/`known_speaker_references` quando a API atual as suportar. Trate falantes que aparecem somente nos trechos posteriores. Não assuma que o rótulo `A` de duas chamadas diferentes representa a mesma pessoa. Quando uma correspondência de voz for incerta, sinalize o segmento para revisão e permita corrigir sua atribuição na interface, sem inventar certeza.

Se o áudio contiver mais de quatro falantes distintos, não misture falas silenciosamente: marque o resultado como necessitando revisão e informe a limitação. Teste conversas com interrupções, silêncio, ruído, falas sobrepostas e troca de falante perto dos limites dos trechos. Preserve erros recuperáveis da API para reprocessamento idempotente; use tentativas com espera crescente e limite de tentativas, sem duplicar cobrança ou entrega.

## 6. Pagamentos e falhas

Implemente o Asaas em sandbox primeiro. Consulte [cobranças avulsas](https://docs.asaas.com/reference/criar-nova-cobranca), [webhooks](https://docs.asaas.com/docs/sobre-os-webhooks) e [estornos](https://docs.asaas.com/reference/estornar-cobranca). Associe o ID remoto de cobrança ao ID local e valide método, valor, moeda, cliente e estado antes de liberar a tarefa. Configure segredo próprio para autenticar webhooks; nunca reutilize a chave da API como `authToken`. Persista cada evento pelo ID único, responda rapidamente e processe em segundo plano. Suporte reentrega, duplicação e chegada fora de ordem sem retroceder estados nem iniciar o job duas vezes. Faça conciliação periódica de cobranças pendentes ou eventos perdidos pela API do Asaas.

Se o pagamento falhar ou expirar, não processe o áudio. Se um pagamento chegar depois de o arquivo ter sido excluído ou de o pedido ter sido cancelado, não processe um arquivo inexistente: inicie o fluxo de estorno. Se a transcrição falhar definitivamente após tentativas controladas, registre a falha, tente estorno integral idempotente via Asaas e acompanhe sua confirmação; se o estorno não puder ser concluído automaticamente, coloque-o em fila de ação administrativa e informe o cliente. Considere que as taxas do gateway podem não ser devolvidas ao comerciante. Para chargeback ou estorno de um resultado já entregue, preserve trilha de auditoria e restrinja novo acesso conforme uma regra documentada, sem apagar os registros financeiros necessários.

## 7. Segurança, privacidade e operação

Áudios e transcrições são privados. Use URLs assinadas de curta duração para reprodução/download, autorização por usuário em toda rota e proteção CSRF nos formulários. Proteja uploads contra arquivos maliciosos e esgotamento de recursos; execute FFmpeg com limites de tempo e recursos, em processo isolado. Implemente limites por usuário/IP para cadastro, upload, cotação e pagamento. Não registre áudio, transcrição, tokens de pagamento nem segredos em logs. Criptografe dados em trânsito e use os recursos de criptografia em repouso do storage/banco.

Exclua automaticamente o **áudio original e derivados de áudio após 7 dias** da conclusão ou cancelamento, inclusive arquivos temporários e trechos. Disponibilize exclusão antecipada solicitada pelo usuário. Transcrições ficam na conta até exclusão solicitada pelo usuário; descreva retenção mínima de registros financeiros e de auditoria separadamente, conforme avaliação jurídica aplicável. Publique política de privacidade, termos de uso e canal de contato antes da operação comercial; trate LGPD como requisito de implementação e revisão jurídica, sem declarar conformidade apenas porque existem páginas legais.

Inclua logs estruturados sem conteúdo sensível, rastreamento por ID de pedido, métricas de filas, duração, erros, custo real e margem, alertas para worker parado/webhook falho/estorno pendente, health checks, backup automático do PostgreSQL e teste de restauração. Documente rotação de chaves e segredos, implantação, rollback e procedimento de reprocessamento administrativo sem cobrança duplicada.

## 8. Modo de trabalho para a IA implementadora

1. **Inventário e contrato:** examine o repositório, proponha estrutura mínima, estados, modelos de dados, interfaces dos provedores, variáveis de ambiente e fluxo de ponta a ponta. Registre premissas no README. Não escolha bibliotecas ou endpoints externos pela memória: confira documentação oficial atual.
2. **Base funcional:** configure Django, PostgreSQL, Redis, Celery, autenticação, storage, Docker Compose, migrações e testes essenciais. Demonstre execução local reproduzível.
3. **Upload e cotação:** implemente validação, storage privado, inspeção de duração, configuração de preços, fórmula e exibição do preço fechado. Teste limites e arredondamento monetário.
4. **Pagamento:** implemente cobrança avulsa Asaas, checkout hospedado, webhook autenticado/idempotente, conciliação e estorno. Teste no sandbox com eventos duplicados, atrasados e fora de ordem.
5. **Transcrição:** implemente preparação de áudio, trechos, diarização, continuidade dos quatro falantes, composição dos segmentos, registros de uso e retries. Teste com áudio representativo em português.
6. **Interface e exportação:** implemente painel do usuário, status, reprodução sincronizada, revisão, renomeação de falantes e TXT/SRT.
7. **Produção:** aplique controles de segurança e retenção, backups, observabilidade, testes integrados, configuração de produção e checklist de implantação. Execute teste ponta a ponta com pagamentos em sandbox e modelo de transcrição real antes de dizer que está pronto.

Em cada etapa, entregue: arquivos alterados, instruções para rodar, testes executados e respectivos resultados, limites conhecidos e próximo passo. Prefira mudanças pequenas e revisáveis. Não use credenciais reais em testes nem as inclua no repositório. Só peça ao operador dados que não possam ser inferidos do código ou da documentação, como chaves, domínio, credenciais de storage, tarifas contratadas e parâmetros comerciais. Não habilite produção sem confirmação de que as chaves, tarifas e webhooks reais foram configurados e testados.

## 9. Critérios mínimos de aceite

- Um arquivo em português de **1h20** é aceito, cotado, pago em sandbox, processado e exportado, com tempos absolutos válidos do início ao fim.
- Um arquivo com **quatro falantes** mantém os mesmos rótulos após as divisões; erros de identificação podem ser corrigidos pelo usuário. Falantes adicionais ou baixa confiança são sinalizados.
- Webhooks de pagamento duplicados, atrasados ou fora de ordem não criam uma segunda tarefa nem liberam processamento sem pagamento válido.
- Falha temporária do provedor gera novas tentativas limitadas; falha definitiva gera estorno ou fila administrativa rastreável, sem perda silenciosa do pedido.
- A cotação salva todos os parâmetros usados; o custo real e a margem final são registrados após o processamento. Diferenças não alteram o preço já pago.
- O áudio e todos os derivados de áudio são excluídos em até sete dias; a exclusão solicitada pelo usuário funciona antes desse prazo. Acesso entre contas é negado.
- Pagamento real só fica disponível após configuração completa do administrador. Não existe em código, interface ou banco qualquer assinatura, plano recorrente ou carteira de créditos.

Ao concluir, apresente evidências desses critérios, o checklist operacional, os riscos restantes e o caminho exato para implantar a aplicação. Não declare que o sistema está pronto para produção enquanto um critério crítico falhar.
