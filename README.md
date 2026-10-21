# Voz em Texto

Monólito Django para transcrição sob demanda em português brasileiro. Cada pedido tem cotação fechada antes do pagamento e cobrança avulsa, sem assinaturas, saldo ou créditos.

## Desenvolvimento

Copie `.env.example` para `.env` e configure os serviços e as chaves. Instale `requirements.txt`, PostgreSQL, Redis e FFmpeg, ou utilize o Docker Compose:

```sh
docker compose up --build
```

Em outro terminal:

```sh
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_dev_pricing
docker compose exec web python manage.py createsuperuser
```

O comando `seed_dev_pricing` exige `DEV_BYPASS_PAYMENT=true`, cria preços ilustrativos para o provedor selecionado e não altera uma tabela de desenvolvimento ativa já existente desse provedor. Não fornece tarifas comerciais. O botão de pagamento de teste cria uma confirmação local, mas as chamadas de transcrição continuam utilizando APIs reais e podem gerar custo.

Para executar o Django fora do Docker, configure banco e Redis acessíveis localmente e utilize:

```sh
python manage.py migrate
python manage.py seed_dev_pricing
python manage.py runserver 0.0.0.0:8000
```

## Provedores e qualidade

Novos pedidos usam Deepgram por padrão:

```dotenv
TRANSCRIPTION_PROVIDER=deepgram
DEEPGRAM_API_KEY=configure-no-ambiente
DEEPGRAM_MODEL=nova-3
DEEPGRAM_LANGUAGE=pt-BR
OPENAI_API_KEY=configure-para-verificacao-opcional
OPENAI_VERIFICATION_MODEL=gpt-transcribe
```

Configure as mesmas variáveis em **web e worker**. A ausência da chave do provedor principal impede a cotação e é verificada novamente antes do pagamento. A ausência da chave OpenAI desabilita a verificação adicional, sem invalidar o resultado principal. Segredos não devem ser enviados ao Git.

O Deepgram recebe o áudio completo em PCM mono de 16 kHz, com `diarize_model=v2`, `utterances=true` e pontuação. Os rótulos de falantes permanecem no contexto da gravação inteira; o rótulo `0` é válido. Os horários e as palavras retornadas são preservados. O áudio original não é substituído pelo MP3 de reprodução.

Para usar o caminho OpenAI como principal, configure `TRANSCRIPTION_PROVIDER=openai`, `OPENAI_TRANSCRIPTION_MODEL=gpt-4o-transcribe-diarize` e `TRANSCRIPTION_LANGUAGE=pt`. Esse caminho utiliza blocos de `TRANSCRIPTION_CHUNK_SECONDS=240`, com `TRANSCRIPTION_OVERLAP_SECONDS=3`, buscando pausas nos dez segundos anteriores ao limite. O tamanho deve ser maior que dez segundos e a sobreposição não negativa e menor que tamanho menos dez. Arquivos por chamada têm limite de 25 MB. Rótulos locais são preservados por bloco; somente referências reconhecidas associam identidades entre chamadas. Até quatro referências temporárias de voz são removidas ao encerrar o processamento.

`webrtcvad-wheels` orienta cortes e procura regiões com voz sem texto. Não remove pausas nem falas baixas. A análise sinaliza lacunas de pelo menos 1,5 segundo, repetições, horários inválidos e possíveis mudanças para inglês. `langdetect`, com semente fixa, analisa passagens longas; expressões inglesas curtas recorrentes servem apenas como indício para verificação.

Trechos suspeitos são verificados em ordem cronológica usando `gpt-transcribe`, `languages=["pt"]` e contexto de transcrição literal. Cada trecho recebe no máximo uma chamada adicional, sem repetição automática do SDK. A soma das durações solicitadas não excede 20% da duração original. A verificação parte do áudio, não traduz o texto produzido.

A aplicação automática exige texto em português, redução do problema de idioma, ausência de novos sinais de repetição, horário válido, falante conhecido e ausência de sobreposição de vozes. Divergências entre números explícitos exigem revisão. Nos demais casos, uma alternativa é disponibilizada para comparação. Falhas, chave ausente e orçamento esgotado mantêm o resultado utilizável e a pendência. Essas regras são heurísticas; não garantem precisão acústica nem distinguem perfeitamente inglês legítimo de erro do reconhecedor.

## Revisão e áudio privado

O editor oferece texto em largura total, filtros sem descarte de edições, motivos recolhidos por trecho, seleção/adição de falantes e revisão individual. A conclusão global é explícita. Apagar o texto de uma fala durante a revisão mantém o campo vazio; a primeira versão permanece disponível para auditoria.

“Ouvir trecho” reproduz o intervalo com um segundo de contexto em cada borda, limitado à duração do arquivo. O player permite pausar, repetir e selecionar velocidade. Uma versão MP3 de 64 kbps é gerada separadamente para reprodução. Para pedidos antigos, a geração é solicitada por POST e executada pelo worker, sem retranscrição.

O acesso ao áudio exige autenticação e propriedade do pedido. No armazenamento local, a resposta suporta HEAD e intervalos HTTP para busca por tempo. No R2 privado, a aplicação redireciona para uma URL assinada com validade de cinco minutos; uma URL já emitida é válida até expirar. O editor pode renovar o acesso. Não exponha `MEDIA_ROOT` publicamente em produção.

A exclusão manual e a limpeza após sete dias removem original e MP3. Um arquivo em preparação não é associado ao pedido se a exclusão ocorrer durante sua geração. Se a preparação falhar, pode ser solicitada novamente; preparações paradas por mais de quinze minutos também podem ser reenfileiradas.

Novos resultados guardam provedor/modelo, rótulo original do falante, palavras e horários, alternativas, motivos de revisão e consumo. Logs não incluem gravações, texto transcrito, respostas de erro dos provedores nem chaves. TXT, SRT e PDF usam as revisões salvas e indicam pendências; os detalhes ficam junto aos respectivos trechos, sem um preâmbulo extenso no PDF.

## Preços e compatibilidade

O provedor e o modelo são fixados no pedido antes da cotação. Mudanças das variáveis não alteram pedidos aceitos. Migrações identificam pedidos anteriores como OpenAI e preservam resultados e edições humanas. Não há reprocessamento automático.

No Admin, selecione o provedor da tabela. Para Deepgram, preencha `api_usd_per_minute`, `verification_usd_per_minute`, câmbio, infraestrutura, tarifas e margem, e valide a configuração. Use as tarifas efetivas de sua conta, incluindo os recursos contratados. A cotação reserva até 20% da duração para a verificação. Para OpenAI principal, permanece disponível o cálculo por tokens. O consumo de verificações e do provedor principal é registrado separadamente e estimado usando a tarifa congelada na cotação, sem cobrança adicional sobre preços já aceitos. Tarifas históricas incompletas geram custo desconhecido, não um valor inventado. Falhas sem retorno de uso podem não aparecer na contabilização local.

## Railway

1. Mantenha PostgreSQL e Redis conectados por `DATABASE_URL` e `REDIS_URL`, com `CELERY_TASK_ALWAYS_EAGER=false`; configure R2 privado com `S3_*` e `S3_ENABLED=true` para acesso compartilhado de web e worker.
2. Atualize **web e worker para o mesmo commit**, com as variáveis dos provedores nos dois serviços.
3. No web, configure o **Pre-deploy Command**:

   ```sh
   python manage.py migrate --noinput
   ```

   Para teste privado com `DEV_BYPASS_PAYMENT=true`, pode manter:

   ```sh
   python manage.py migrate --noinput && python manage.py seed_dev_pricing
   ```

4. O web utiliza `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --timeout 120`. O worker utiliza `celery -A config worker -l info --concurrency=1`. A tarefa de áudio longo executa no worker; não aumente o tempo HTTP para acomodá-la. Para exclusão automática e conciliação, mantenha `celery -A config beat -l info`.
5. Aguarde migrações e ambos os deploys terminarem. Configure `/health/` como health check, domínio/TLS, `PUBLIC_BASE_URL`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DEBUG=false`, `ALLOW_SIGNUPS=false` e `SECURE_SSL_REDIRECT=true`. Não inclua segredos ou gravações no Git.
6. Para operação comercial, valide as tarifas reais e desative `DEV_BYPASS_PAYMENT`; remova `seed_dev_pricing` do pre-deploy. Mantenha SMTP, Asaas, webhook `/pagamentos/webhook/asaas/`, backups do PostgreSQL e as informações legais do serviço configurados.

## Testes e comparação de qualidade

```sh
python manage.py test
```

A suíte cobre os adaptadores com respostas simuladas, migrações, preços, falante zero, preservação de rótulos, verificações limitadas, edição, segurança e exclusão do áudio. A preparação e reprodução usam FFmpeg real em testes locais.

Os testes de navegador são habilitados apontando `PLAYWRIGHT_MODULE` para uma instalação de Playwright com Chromium:

```sh
PLAYWRIGHT_MODULE=/caminho/node_modules/playwright python manage.py test jobs.test_browser
```

Eles verificam desktop/celular, largura dos campos, filtros, persistência de edição, reprodução, parada, repetição, velocidade e renovação da URL. Testes simulados verificam o fluxo e não medem fidelidade acústica.

Para comparar explicitamente os provedores em um arquivo local consentido:

```sh
python manage.py compare_transcription --audio /caminho/amostra.wav --output /caminho/relatorio-novo.json
```

Esse comando faz chamadas pagas, exige ambas as chaves, não executa verificação adicional, não altera pedidos e não sobrescreve arquivos existentes. O relatório contém transcrições, pendências, consumo e tempo por provedor e é criado com permissão `0600`. Mantenha-o fora do Git e de diretórios públicos. Pode limitar a comparação com `--providers deepgram` ou `--providers openai`.

Compare amostras do início, meio, final e trechos problemáticos, incluindo 01:18–01:20 do teste original, com uma referência humana. Confira palavras omitidas, inseridas e substituídas, números, idioma, falantes, tempo e custo. A avaliação acústica depende do áudio original e das chaves reais; não é substituída pelos testes automatizados.
