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
