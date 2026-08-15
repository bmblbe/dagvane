# Архів active-документації до reset

> **Historical, non-authoritative.** Файли в цьому каталозі збережені лише як
> evidence попереднього controller state. Вони містять застарілі status claims
> і не є інструкціями для запуску або розробки.

- Source branch: `main`
- Source SHA: `324f6c51cf7a68a8a8ad61529147873deef5a3d2`
- Snapshot date: 2026-08-15
- Archived during documentation reset: 2026-08-16

## Path mapping

| Попередній active path | Архівна копія | SHA-256 | Новий канон |
|---|---|---|---|
| `/README.md` | `root/README.md` | `e10c478dbc4791f21304e0af24db301b499ffcb33d7d4df26f86adadf75eac68` | `/README.md` |
| `/DEVELOPMENT.md` | `root/DEVELOPMENT.md` | `5a310dcbce8b10661d297edb8ad2c73034dd1ddf780592f81f41d47a5c4932a2` | `/DEVELOPMENT.md` |
| `docs/architecture/README.md` | `architecture/README.md` | `b401d1f99c93677447c2766295d6d2e2be67a75eb7248c22a7f147e6c235b7fb` | `docs/ARCHITECTURE.md` |
| `docs/architecture/modules/README.md` | `architecture/modules/README.md` | `0329c8649f07bb74f938a39983a62859e2c455b687d7f14d3c79c81fe6f74ee4` | `docs/MODULES.md` |
| `docs/architecture/modules/autodev/ARCHITECTURE.md` | `architecture/modules/autodev/ARCHITECTURE.md` | `b7c07ca77f4e7e0bdc7ea6239e4db179354614a2d57584579dd3b2ef0e1ccab8` | `docs/ARCHITECTURE.md`, `docs/TODO.md` |
| `docs/architecture/modules/backends/PLAN.md` | `architecture/modules/backends/PLAN.md` | `f9cf7d9fbbef9e4ef597650de591125bdad9df87a3cb87f436a56571b54da5f0` | `docs/DEVELOPMENT_PLAN.md` |
| `docs/development/CURRENT_STATE.md` | `development/CURRENT_STATE.md` | `80925aa059a301ede5d9baa8c8c59ad7d5fc19381ba744e2f075aabd094b5eb3` | `docs/TODO.md` |
| `docs/development/ORCHESTRAL_WORKFLOW.md` | `development/ORCHESTRAL_WORKFLOW.md` | `6e1ac1c7ce944b6328be2a7668ae835814c7ff181ca5bf33afd15ac66854a946` | `/DEVELOPMENT.md` |
| `docs/implementation/MASTER_PLAN.md` | `implementation/MASTER_PLAN.md` | `0e2376adb3b02d41623df4530d18cbf4d9d85ba05e0bb48589a22ec08ca69a03` | `docs/DEVELOPMENT_PLAN.md` |

Прийнятий backend contract
`docs/architecture/modules/backends/ARCHITECTURE.md`, ADR у
`docs/architecture/decisions/**` та immutable research evidence у
`docs/architecture/history/**` не архівувалися і не редагувалися.

Деякі immutable ADR посилаються на колишні operational paths. Самі ADR не
переписуються; таблиця вище є mapping для таких historical references. Active
документи не повинні посилатися на цей архів як на normative source.
