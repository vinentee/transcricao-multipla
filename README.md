# Transcrição múltipla

Monólito Django para transcrição sob demanda em português brasileiro. Cada pedido tem uma cotação fechada antes do pagamento e uma cobrança avulsa; não existem assinaturas, saldo nem créditos.

## Execução local

```sh
cp .env.example .env
docker compose up --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

Abra `http://localhost:8000`. Para rodar fora do Docker: crie um ambiente virtual, instale `requirements.txt`, defina `CELERY_TASK_ALWAYS_EAGER=true` para desenvolvimento simples, execute `python manage.py migrate`, o servidor Django e um worker Celery.

### Teste local sem Asaas, R2 ou SMTP

Com `DEBUG=true`, `ALLOW_SIGNUPS=true`, `S3_ENABLED=false`, `DEV_BYPASS_PAYMENT=true` e uma `OPENAI_API_KEY` configurada, execute:

```sh
python manage.py migrate
python manage.py seed_dev_pricing
python manage.py runserver 0.0.0.0:8000
```

O botão **Pagamento de teste** aparece apenas nessas condições e cria uma cobrança local recebida. O endpoint é deliberadamente indisponível quando `DEBUG=false`. Para acesso de outro dispositivo, acrescente `.trycloudflare.com` a `ALLOWED_HOSTS`, `https://*.trycloudflare.com` a `CSRF_TRUSTED_ORIGINS` e execute `cloudflared tunnel --url http://127.0.0.1:8000`.

## Configuração operacional

- Crie um bucket R2 privado e aplique CORS apenas ao domínio público. Configure `S3_*` e `S3_ENABLED=true`; objetos são acessados apenas pelo backend.
- Configure uma chave de projeto OpenAI e `OPENAI_TRANSCRIPTION_MODEL=gpt-4o-transcribe-diarize`. A referência consultada em 13/10/2026 indica o endpoint de transcrições, `diarized_json`, `chunking_strategy=auto` em arquivos longos e limite de 25 MB por chamada. A tabela de preços deve ser preenchida e validada no Admin antes de aceitar cobranças.
- Crie uma conta sandbox Asaas, configure `ASAAS_API_KEY`, um `ASAAS_WEBHOOK_TOKEN` exclusivo e o webhook público em `/pagamentos/webhook/asaas/`. O token é validado no cabeçalho `asaas-access-token`; eventos são persistidos por ID e processados assincronamente.
- Preencha SMTP de produção por variáveis de ambiente. Não inclua segredos, gravações ou dados de pagamento no Git.

## Railway (publicação rápida)

1. Crie PostgreSQL e Redis gerenciados no projeto Railway e conecte `DATABASE_URL`/`REDIS_URL` aos serviços.
2. Para o teste privado, crie dois serviços a partir deste repositório e da mesma imagem: `web` com `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --timeout 120` e `worker` com `celery -A config worker -l info --concurrency=1`. O `beat` pode ser adicionado depois com `celery -A config beat -l info` quando a limpeza automática e a conciliação real forem necessárias.
3. Execute `python manage.py migrate` uma vez como comando de release; exponha somente o serviço web e configure `/health/` como health check.
4. Adicione domínio/TLS, configure `PUBLIC_BASE_URL`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DEBUG=false`, `ALLOW_SIGNUPS=false`, `SECURE_SSL_REDIRECT=true`, `SECURE_HSTS_SECONDS=3600`, `SECURE_HSTS_INCLUDE_SUBDOMAINS=true`, `SECURE_HSTS_PRELOAD=true` e segredos. Para o teste com cobrança mocada, use `DEV_BYPASS_PAYMENT=true`; nunca habilite essa opção na operação comercial. Mantenha banco, Redis e R2 privados.
5. Configure backup diário do PostgreSQL para storage separado e teste restauração antes de habilitar pagamentos reais.

## Limites e checklist antes de produção

- O fluxo de transcrição requer chave OpenAI, configuração de tarifa validada no Admin e áudio de teste consentido de 1h20/4 falantes.
- Os textos legais em `/legal/` são modelos e exigem revisão jurídica/LGPD e identificação do controlador.
- Valide o webhook externo, conciliação Asaas, estorno, backup/restauração e exclusão de áudio após sete dias no ambiente sandbox antes de trocar chaves reais.
- A documentação oficial deve ser revisada novamente imediatamente antes da ativação comercial: [OpenAI](https://developers.openai.com/api/docs/guides/speech-to-text), [Asaas](https://docs.asaas.com/docs/sobre-os-webhooks) e [R2](https://developers.cloudflare.com/r2/api/s3/presigned-urls/).


## Fidelidade, falantes e revisão de áudios longos

O fluxo envia `TRANSCRIPTION_LANGUAGE=pt` em todas as chamadas do modelo
`gpt-4o-transcribe-diarize`, sem `prompt` (não suportado pelo modelo).
`TRANSCRIPTION_CHUNK_SECONDS=240` e `TRANSCRIPTION_OVERLAP_SECONDS=3`
controlam blocos e contexto nas bordas. A duração precisa ser maior que 10
segundos e a sobreposição deve ser não negativa e menor que duração menos 10.
Cada arquivo enviado deve ter menos de 25 MB; aumentos excessivos do bloco são
rejeitados antes da chamada. A duração máxima do pedido continua sendo duas horas.

O áudio é decodificado uma vez para PCM mono de 16 kHz. `webrtcvad-wheels`
orienta os cortes, buscando pausas nos dez segundos anteriores ao limite.
A detecção de voz não remove pausas nem falas baixas: todo o áudio é enviado.
Cortes e amostras de referência são extraídos do PCM, sem recompressões sucessivas.

A união preserva falas simultâneas. Apenas textos completos equivalentes,
com tempos próximos e a mesma identidade de voz, são removidos nas
sobreposições de blocos vizinhos. Versões divergentes são preservadas para
revisão; não há alinhamento inventado por palavra. Rótulos locais retornados
pela API não identificam a mesma pessoa automaticamente entre chamadas.

Até quatro amostras de voz de 2 a 10 segundos, sem outra fala sobreposta e
com pelo menos 1,5 segundo de atividade de voz, são extraídas de trechos sem
alertas e enviadas como referências nas próximas chamadas. São amostras do
próprio áudio, processadas pelo mesmo provedor. Nenhuma amostra ou dado base64
é persistido no banco ou nos logs. Os temporários são removidos ao sair do
processamento, inclusive em falhas. Falantes que não puderem ser associados
são apresentados como “Falante não identificado”; suas falas são preservadas.
O limite de quatro referências não limita a quantidade de falas salvas.

A verificação sinaliza texto vazio, horários inválidos, voz sem texto por
pelo menos 1,5 segundo, sequências de quatro palavras repetidas quatro vezes
seguidas e possível mudança para inglês. `langdetect`, com semente fixa,
avalia passagens de pelo menos 20 palavras e exige probabilidade de inglês
mínima de 90%. Essas verificações são heurísticas: podem deixar passar erros
ou sinalizar trechos corretos, especialmente em gravações multilíngues.

Cada bloco suspeito pode ter **uma** chamada adicional, com o mesmo áudio,
contexto das bordas e referências já disponíveis. A troca exige menos problemas
detectados e nenhuma perda de cobertura de voz; empate mantém o original.
Falhas da verificação adicional mantêm o primeiro resultado para revisão.
No pior caso, todos os blocos terão duas chamadas. O consumo retornado por
todas as chamadas é somado, sem alterar o preço aceito nem gerar nova cobrança.
Chamadas que falhem antes de retornar uso podem não ser contabilizadas localmente.
Não se traduz nem se completa automaticamente o texto produzido.

`AudioJob.review_reasons` guarda códigos e intervalos dos alertas;
`TranscriptSegment.review_reasons` guarda os códigos do trecho.
`quality_metadata` registra versão do fluxo, idioma, blocos e novas tentativas.
O editor e as exportações indicam revisão pendente, inclusive lacunas sem
segmentos. Na exportação SRT o aviso geral aparece na primeira legenda;
os intervalos e motivos completos ficam no editor, TXT e PDF.
Salvar texto editado não remove os alertas. Após conferir o áudio, o usuário
pode confirmar a conclusão da revisão para limpar as sinalizações.

### Atualização e validação

Instale `requirements.txt`, execute `python manage.py migrate` e reinicie os
processos web e Celery. A migração acrescenta campos com valores vazios,
preservando resultados existentes e revisões manuais. Pedidos concluídos não
são reprocessados automaticamente. As regras de pagamento e estorno não mudam.

Execute `python manage.py test`. A suíte inclui preparação real via FFmpeg,
limites equivalentes a duas horas, união de bordas, falas simultâneas,
referências, detecção de idioma, tentativas limitadas, consumo e alertas.
Os testes usam respostas simuladas da API e não avaliam fidelidade acústica.
Antes de validar a qualidade em produção, compare com uma transcrição humana
amostras do início, meio, final e limites dos blocos do áudio de teste, incluindo
01:18–01:20. Confira palavras omitidas, inseridas, substituídas, valores numéricos,
identidade dos falantes e mudanças indevidas de idioma. Os detectores não
representam uma garantia de transcrição perfeita.
