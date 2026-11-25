# Instruções de revisão de código

Critérios usados pelos revisores automáticos deste repositório: o agente `pr-reviewer`
(Claude, antes de cada push, publicado no PR como o status `pr-reviewer`) e o
GitHub Copilot code review, se ele for habilitado.
Escreva os achados em português. Edite a última seção com as regras do projeto.

## O que revisar, em ordem de prioridade

1. **Correção:** bugs, casos de borda, erros engolidos, condições de corrida e contratos quebrados.
2. **Segurança:** injeção (SQL, shell, path), segredos no código ou em logs, validação de
   entrada, autorização, desserialização insegura e dependências vulneráveis.
3. **Testes:** todo comportamento novo ou alterado tem teste; bug corrigido tem teste de
   regressão; nada de `skip`/`xfail`/`.only`, asserções enfraquecidas ou cobertura reduzida.
4. **Dados e compatibilidade:** migrações reversíveis; mudança incompatível marcada com
   `BREAKING CHANGE:`; nenhuma perda silenciosa de dados.
5. **Desempenho:** consultas N+1, laços quadráticos sobre dados grandes e I/O desnecessário.
6. **Manutenibilidade:** código morto, duplicação relevante, nomes enganosos e
   complexidade sem justificativa.
7. **Convenções:** Conventional Commits em inglês, branch `<tipo>/<kebab-case>`, um commit
   por unidade reversível e nenhuma mudança fora do escopo do PR.

## O que não comentar

- Formatação que o formatter ou o linter já resolvem.
- Preferências pessoais sem impacto concreto.
- Elogios ou resumos do diff.

## Severidade

| Severidade | Quando | Bloqueia? |
|---|---|---|
| `critical` | falha de segurança, perda de dados ou crash num fluxo principal | sim |
| `major` | comportamento incorreto, contrato quebrado ou lógica nova sem teste | sim |
| `minor` | caso de borda ou risco de manutenção | não |
| `nit` | polimento opcional | não |

Todo achado traz arquivo, linha, o porquê do problema e uma correção concreta.

## Regras específicas deste repositório

- (adicione aqui)
