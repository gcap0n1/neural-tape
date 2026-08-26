# Neural Tape VS Code Extension — Product Requirements Document

**Version:** 2.1 (Realistico)
**Status:** Draft — Post-Verifica API
**Author:** Lex (AI Agent) for Guglielmo
**Date:** 2026-07-08
**Language:** Italian (content), English (code/comments)
**License:** MIT

---

## 1. Executive Summary

Il PRD v2.0 era costruito su assunzioni tecniche false. Dopo verifica API VS Code:

- `vscode.chat.onDidReceiveMessage` **NON ESISTE**
- Cattura messaggi Copilot/Unify **IMPOSSIBILE per design** (privacy)
- `addSystemMessage` per inject context **NON ESISTE**
- Alert inline nella chat di Copilot **IMPOSSIBILE**

Questo PRD v2.1 ricostruisce Neural Tape su **API realmente esistenti**, mantenendo il 90% del valore con il 30% dello sforzo.

---

## 2. Vision & Goals (Aggiornati)

### 2.1 Problem Statement

Neural Tape v1.2 (Python) funziona ma ha limiti:
- Watchdog su file: latenza, fragile, dipende dal formato log
- UI review nel terminale: grezza, non integrata nell'IDE
- Deja Vu alert silenzioso: solo in file, non visibile
- Pre-load manuale: l'utente deve ricordarsi di eseguirlo

### 2.2 Vision

Neural Tape v2.1 = **Layer UI + Intelligence nativo VS Code** sopra il log-parser Python funzionante.

Non sostituisce il log-parser: lo **completa** con:
- Status bar visibile
- Review interattiva in webview
- Popup Deja Vu
- Pre-load con un click
- `@neural-tape` participant per sessioni critiche (opt-in)

### 2.3 Goals Realistici

| Priority | Goal | Metric | Fattibilita |
|----------|------|--------|-------------|
| P0 | Status bar con contatore insights | < 1s aggiornamento | ✅ VS Code API |
| P0 | Leggere staging/archive esistenti (v1.2) | Compatibilita 100% | ✅ File system |
| P1 | Review interattiva in webview/QuickPick | UI nativa | ✅ Webview API |
| P1 | Pre-load command (manuale) | Genera session-context.md | ✅ File system |
| P1 | Deja Vu alert come popup VS Code | Notifica nativa | ✅ showInformationMessage |
| P2 | `@neural-tape` Chat Participant | Cattura esplicita opt-in | ✅ createChatParticipant |
| P2 | Terminal error capture (opzionale) | Shell integration | ⚠️ Limitato |
| P3 | Auto-preload all'avvio workspace | Event-driven | ✅ onDidChangeWorkspaceFolders |

### 2.4 Non-Goals

- **NOT** cattura passiva di Copilot/Unify — architetturalmente impossibile
- **NOT** inject automatico nel system prompt — API inesistente
- **NOT** alert inline nella chat di Copilot — non si puo scrivere nel thread altrui
- **NOT** replacement del log-parser Python — lo si integra, non si butta

---

## 3. Architecture

### 3.1 System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         VS CODE                                     │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  CHAT AI (Unify / Copilot / @neural-tape)                    │   │
│  │  ──────────────────────────────────────                     │   │
│  │  A. Copilot/Unify normale → log scritti da assistente       │   │
│  │     → log-parser Python li legge (come ora)                 │   │
│  │                                                               │   │
│  │  B. @neural-tape participant → cattura ESPLICITA              │   │
│  │     → prompt + risposta + tool calls salvati direttamente   │   │
│  │     → NO dipendenza dal formato log                         │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                              │                                      │
│                              ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │           NEURAL TAPE EXTENSION (TypeScript)                │   │
│  │  ───────────────────────────────────────────                  │   │
│  │                                                               │   │
│  │  UI LAYER (100% fattibile)                                    │   │
│  │  ├── StatusBar: $(brain) NT: N insights                      │   │
│  │  ├── QuickPick: Review promote/skip/modify                 │   │
│  │  ├── WebviewPanel: Review completa (futuro)                  │   │
│  │  └── Notifications: Deja Vu popup                           │   │
│  │                                                               │   │
│  │  INTELLIGENCE LAYER (legge file esistenti)                  │   │
│  │  ├── PreLoadCommand: genera session-context.md               │   │
│  │  ├── PostCaptureCommand: review staging                     │   │
│  │  └── DejaVuCheck: confronta staging vs archive              │   │
│  │                                                               │   │
│  │  PARTICIPANT LAYER (opt-in, esplicito)                        │   │
│  │  └── @neural-tape: cattura prompt/risposta/tool calls        │   │
│  │      → salva direttamente in staging (NO log-parser)          │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                              │                                      │
│                              ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  STORAGE (compatibile v1.2, stesso formato)                  │   │
│  │  ─────────────────────────────────────────                   │   │
│  │  Workspace: .vscode/neural-tape/tape/staging/               │   │
│  │  Global: ~/.neural-tape/tape/archive/{type}/                │   │
│  │  Context: .vscode/neural-tape/session-context.md            │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                              │                                      │
│                              ▼                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  LOG PARSER PYTHON v1.2 (rimane invariato)                   │   │
│  │  ─────────────────────────────────────────                   │   │
│  │  • Watchdog su file log dell'assistente                     │   │
│  │  • Pattern matching regex                                   │   │
│  │  • Scrive in tape/staging/                                  │   │
│  │  • Funziona per Kimi Code, OpenCode, ZCode, etc.          │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 Canali di Cattura

| Canale | Come funziona | Quando usarlo | Pro | Contro |
|--------|---------------|---------------|-----|--------|
| **A. Log Parser Python** | Watchdog su file log | Default, sempre attivo | Funziona con qualsiasi assistente | Dipende dal formato log, latenza |
| **B. @neural-tape Participant** | Hook diretto su prompt/risposta | Sessioni critiche, cattura garantita | Zero dipendenza dal formato log, metadata completi | L'utente deve chiamare esplicitamente `@neural-tape` |
| **C. Entrambi** | A in background + B quando chiamato | Massima copertura | Fallback automatico | Deduplicazione necessaria tra A e B |

### 3.3 Flusso Utente Completo

```
Apri VS Code in EterCervo
    │
    ▼
Lex (agente custom) esegue pre-load
→ Genera session-context.md
→ Mostra stato: sessione #N, TODO, insight
→ Chiede: "Quale canale? [A] Terminale [B] @neural-tape [C] Ibrido"
    │
    ├──► [A] Terminale/Kimi Code/OpenCode
    │      → Avvia log-parser Python
    │      → Lavori normalmente
    │      → A fine sessione: end-sessions.sh → review CLI
    │
    ├──► [B] VS Code Chat + @neural-tape
    │      → Scrivi nella chat: "@neural-tape fix the CORS error"
    │      → Participant cattura prompt, gira a GLM-5.2
    │      → Cattura risposta, tool calls, metadata
    │      → Salva in staging
    │      → A fine: comando "Neural Tape: End Session" → review webview
    │
    └──► [C] Ibrido (consigliato)
           → Log-parser Python avviato in background
           → Per task critici: @neural-tape
           → Deduplicazione automatica (SHA-256)
           → Review unificata
```

---

## 4. Component Specification

### 4.1 Extension Manifest (package.json)

```json
{
  "name": "neural-tape",
  "displayName": "Neural Tape",
  "description": "Passive memory layer for AI coding — UI + Intelligence for VS Code",
  "version": "2.1.0",
  "publisher": "guglielmo-caponi",
  "license": "MIT",
  "engines": { "vscode": "^1.90.0" },
  "categories": ["Machine Learning", "Other"],
  "keywords": ["ai", "memory", "assistant", "chat", "context", "neural-tape"],

  "activationEvents": [
    "onStartupFinished",
    "onChatParticipant:neural-tape"
  ],

  "main": "./out/extension.js",

  "contributes": {
    "chatParticipants": [
      {
        "id": "neural-tape",
        "name": "neural-tape",
        "description": "Activate Neural Tape capture for this session",
        "isSticky": true,
        "commands": [
          {
            "name": "endSession",
            "description": "End session and review captured insights"
          },
          {
            "name": "reviewStaging",
            "description": "Review insights in staging"
          }
        ]
      }
    ],

    "commands": [
      {
        "command": "neural-tape.pre-load",
        "title": "Neural Tape: Pre-load Context",
        "category": "Neural Tape",
        "icon": "$(brain)"
      },
      {
        "command": "neural-tape.post-capture",
        "title": "Neural Tape: Review Session",
        "category": "Neural Tape",
        "icon": "$(check)"
      },
      {
        "command": "neural-tape.open-review",
        "title": "Neural Tape: Open Review Panel",
        "category": "Neural Tape",
        "icon": "$(eye)"
      },
      {
        "command": "neural-tape.toggle-capture",
        "title": "Neural Tape: Toggle Capture",
        "category": "Neural Tape",
        "icon": "$(record)"
      },
      {
        "command": "neural-tape.clear-staging",
        "title": "Neural Tape: Clear Staging",
        "category": "Neural Tape",
        "icon": "$(trash)"
      },
      {
        "command": "neural-tape.check-deja-vu",
        "title": "Neural Tape: Check Deja Vu",
        "category": "Neural Tape",
        "icon": "$(warning)"
      },
      {
        "command": "neural-tape.select-channel",
        "title": "Neural Tape: Select Channel",
        "category": "Neural Tape",
        "icon": "$(server)"
      }
    ],

    "menus": {
      "commandPalette": [
        { "command": "neural-tape.pre-load" },
        { "command": "neural-tape.post-capture" },
        { "command": "neural-tape.open-review" },
        { "command": "neural-tape.toggle-capture" },
        { "command": "neural-tape.clear-staging" },
        { "command": "neural-tape.check-deja-vu" },
        { "command": "neural-tape.select-channel" }
      ]
    },

    "configuration": {
      "title": "Neural Tape",
      "properties": {
        "neuralTape.enabled": {
          "type": "boolean",
          "default": true,
          "description": "Enable Neural Tape"
        },
        "neuralTape.channel": {
          "type": "string",
          "enum": ["auto", "python", "participant", "hybrid"],
          "default": "hybrid",
          "description": "Default capture channel"
        },
        "neuralTape.storage.globalPath": {
          "type": "string",
          "default": "~/.neural-tape",
          "description": "Global storage path for archive"
        },
        "neuralTape.storage.workspacePath": {
          "type": "string",
          "default": ".vscode/neural-tape",
          "description": "Workspace-relative path for staging"
        },
        "neuralTape.preLoad.maxInsights": {
          "type": "number",
          "default": 10,
          "minimum": 1,
          "maximum": 50
        },
        "neuralTape.preLoad.maxPatterns": {
          "type": "number",
          "default": 5,
          "minimum": 1,
          "maximum": 20
        },
        "neuralTape.preLoad.autoRun": {
          "type": "boolean",
          "default": true,
          "description": "Auto-run pre-load on workspace open"
        },
        "neuralTape.dejaVu.threshold": {
          "type": "number",
          "default": 0.75,
          "minimum": 0.0,
          "maximum": 1.0
        },
        "neuralTape.dejaVu.enabled": {
          "type": "boolean",
          "default": true
        },
        "neuralTape.dedup.ttlMinutes": {
          "type": "number",
          "default": 5,
          "minimum": 1,
          "maximum": 60
        },
        "neuralTape.decay.halfLifeDays": {
          "type": "number",
          "default": 7,
          "minimum": 1,
          "maximum": 30
        },
        "neuralTape.privacy.enabled": {
          "type": "boolean",
          "default": true
        },
        "neuralTape.patterns.shellError.enabled": {
          "type": "boolean",
          "default": true
        },
        "neuralTape.patterns.contextCompaction.enabled": {
          "type": "boolean",
          "default": true
        },
        "neuralTape.patterns.largeOutput.enabled": {
          "type": "boolean",
          "default": true
        },
        "neuralTape.patterns.longReasoning.enabled": {
          "type": "boolean",
          "default": true
        },
        "neuralTape.patterns.fileWrite.enabled": {
          "type": "boolean",
          "default": false
        }
      }
    }
  },

  "scripts": {
    "vscode:prepublish": "npm run compile",
    "compile": "tsc -p ./",
    "watch": "tsc -watch -p ./",
    "lint": "eslint src --ext ts"
  },

  "devDependencies": {
    "@types/node": "^20.0.0",
    "@types/vscode": "^1.90.0",
    "@typescript-eslint/eslint-plugin": "^7.0.0",
    "@typescript-eslint/parser": "^7.0.0",
    "eslint": "^8.57.0",
    "typescript": "^5.4.0"
  },

  "dependencies": {
    "js-yaml": "^4.1.0"
  }
}
```

### 4.2 Extension Entry Point (src/extension.ts)

```typescript
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { StatusBarManager } from './ui/statusBar';
import { PreLoadCommand } from './commands/preLoad';
import { PostCaptureCommand } from './commands/postCapture';
import { ReviewPanel } from './ui/reviewPanel';
import { DejaVuChecker } from './intelligence/dejaVu';
import { NeuralTapeParticipant } from './participant/neuralTapeParticipant';
import { StagingManager } from './storage/stagingManager';

let statusBar: StatusBarManager;
let participant: NeuralTapeParticipant;

export function activate(context: vscode.ExtensionContext): void {
    console.log('[Neural Tape] Activating v2.1...');

    // 1. Initialize storage paths
    const globalPath = resolvePath(
        vscode.workspace.getConfiguration('neuralTape').get('storage.globalPath', '~/.neural-tape')
    );
    const workspacePath = getWorkspacePath();

    if (!workspacePath) {
        console.log('[Neural Tape] No workspace open, skipping activation');
        return;
    }

    // 2. Ensure directories exist
    ensureDirectories(globalPath, workspacePath);

    // 3. Status Bar
    statusBar = new StatusBarManager(workspacePath);
    statusBar.show();
    context.subscriptions.push(statusBar);

    // 4. Commands
    registerCommands(context, globalPath, workspacePath);

    // 5. Chat Participant (@neural-tape)
    participant = new NeuralTapeParticipant(globalPath, workspacePath);
    participant.register(context);

    // 6. Auto pre-load on workspace open
    const config = vscode.workspace.getConfiguration('neuralTape');
    if (config.get('preLoad.autoRun', true)) {
        runAutoPreload(workspacePath, globalPath);
    }

    // 7. Watch staging for real-time updates
    watchStaging(workspacePath, statusBar);

    console.log('[Neural Tape] Activated successfully');
}

export function deactivate(): void {
    console.log('[Neural Tape] Deactivating...');
    statusBar?.dispose();
    participant?.dispose();
}

// ─── Helpers ─────────────────────────────────────────────────────────

function resolvePath(inputPath: string): string {
    if (inputPath.startsWith('~/')) {
        return path.join(process.env.HOME || process.env.USERPROFILE || '', inputPath.slice(2));
    }
    return inputPath;
}

function getWorkspacePath(): string | undefined {
    const folders = vscode.workspace.workspaceFolders;
    return folders?.[0]?.uri.fsPath;
}

function ensureDirectories(globalPath: string, workspacePath: string): void {
    const dirs = [
        path.join(globalPath, 'tape', 'archive'),
        path.join(globalPath, 'tape', 'archive', 'bug_found'),
        path.join(globalPath, 'tape', 'archive', 'eureka'),
        path.join(globalPath, 'tape', 'archive', 'warning'),
        path.join(globalPath, 'tape', 'archive', 'code_change'),
        path.join(workspacePath, '.vscode', 'neural-tape', 'tape', 'staging'),
        path.join(workspacePath, '.vscode', 'neural-tape', 'tape', 'sessions'),
    ];
    dirs.forEach(d => fs.mkdirSync(d, { recursive: true }));
}

function registerCommands(
    context: vscode.ExtensionContext,
    globalPath: string,
    workspacePath: string
): void {
    // Pre-load
    context.subscriptions.push(
        vscode.commands.registerCommand('neural-tape.pre-load', async () => {
            const cmd = new PreLoadCommand(globalPath, workspacePath);
            const result = await cmd.execute();
            vscode.window.showInformationMessage(`Neural Tape: ${result}`);
        })
    );

    // Post-capture review
    context.subscriptions.push(
        vscode.commands.registerCommand('neural-tape.post-capture', async () => {
            const cmd = new PostCaptureCommand(globalPath, workspacePath);
            await cmd.execute();
            statusBar.refresh();
        })
    );

    // Open review panel
    context.subscriptions.push(
        vscode.commands.registerCommand('neural-tape.open-review', async () => {
            const panel = new ReviewPanel(globalPath, workspacePath);
            await panel.show();
        })
    );

    // Toggle capture
    context.subscriptions.push(
        vscode.commands.registerCommand('neural-tape.toggle-capture', () => {
            const config = vscode.workspace.getConfiguration('neuralTape');
            const current = config.get('enabled', true);
            config.update('enabled', !current, true);
            vscode.window.showInformationMessage(
                `Neural Tape: ${!current ? 'Enabled' : 'Disabled'}`
            );
        })
    );

    // Clear staging
    context.subscriptions.push(
        vscode.commands.registerCommand('neural-tape.clear-staging', async () => {
            const staging = new StagingManager(workspacePath);
            await staging.clear();
            statusBar.refresh();
            vscode.window.showInformationMessage('Neural Tape: Staging cleared');
        })
    );

    // Check Deja Vu
    context.subscriptions.push(
        vscode.commands.registerCommand('neural-tape.check-deja-vu', async () => {
            const checker = new DejaVuChecker(globalPath);
            const alerts = await checker.checkStaging(workspacePath);
            if (alerts.length > 0) {
                const top = alerts[0];
                vscode.window.showWarningMessage(
                    `Deja Vu: ${top.similarity}% similar to archived insight`,
                    'View Archive',
                    'Dismiss'
                ).then(action => {
                    if (action === 'View Archive') {
                        const uri = vscode.Uri.file(top.archivedFile);
                        vscode.workspace.openTextDocument(uri).then(doc => {
                            vscode.window.showTextDocument(doc);
                        });
                    }
                });
            } else {
                vscode.window.showInformationMessage('Neural Tape: No Deja Vu detected');
            }
        })
    );

    // Select channel
    context.subscriptions.push(
        vscode.commands.registerCommand('neural-tape.select-channel', async () => {
            const selection = await vscode.window.showQuickPick(
                [
                    { label: '$(terminal) Python Log Parser', description: 'Watchdog on log files (default)', value: 'python' },
                    { label: '$(comment-discussion) @neural-tape Participant', description: 'Explicit capture in chat', value: 'participant' },
                    { label: '$(layers) Hybrid (both)', description: 'Maximum coverage', value: 'hybrid' }
                ],
                { placeHolder: 'Select Neural Tape capture channel' }
            );
            if (selection) {
                const config = vscode.workspace.getConfiguration('neuralTape');
                await config.update('channel', selection.value, true);
                vscode.window.showInformationMessage(`Neural Tape: Channel set to ${selection.label}`);
            }
        })
    );
}

async function runAutoPreload(workspacePath: string, globalPath: string): Promise<void> {
    try {
        const cmd = new PreLoadCommand(globalPath, workspacePath);
        await cmd.execute();
        console.log('[Neural Tape] Auto pre-load completed');
    } catch (err) {
        console.error('[Neural Tape] Auto pre-load failed:', err);
    }
}

function watchStaging(workspacePath: string, statusBar: StatusBarManager): void {
    const stagingPath = path.join(workspacePath, '.vscode', 'neural-tape', 'tape', 'staging');

    if (!fs.existsSync(stagingPath)) return;

    const watcher = fs.watch(stagingPath, (eventType, filename) => {
        if (filename && filename.endsWith('.md')) {
            statusBar.refresh();
        }
    });

    // Initial count
    statusBar.refresh();
}
```

### 4.3 Status Bar (src/ui/statusBar.ts)

```typescript
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

export class StatusBarManager {
    private statusBarItem: vscode.StatusBarItem;
    private workspacePath: string;
    private stagingCount: number = 0;

    constructor(workspacePath: string) {
        this.workspacePath = workspacePath;
        this.statusBarItem = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Right,
            100
        );
        this.statusBarItem.command = 'neural-tape.open-review';
    }

    show(): void {
        this.refresh();
        this.statusBarItem.show();
    }

    refresh(): void {
        this.stagingCount = this.countStagingFiles();
        this.updateDisplay();
    }

    private countStagingFiles(): number {
        const stagingDir = path.join(
            this.workspacePath,
            '.vscode',
            'neural-tape',
            'tape',
            'staging'
        );
        if (!fs.existsSync(stagingDir)) return 0;

        try {
            return fs.readdirSync(stagingDir)
                .filter(f => f.endsWith('.md'))
                .length;
        } catch {
            return 0;
        }
    }

    private updateDisplay(): void {
        if (this.stagingCount > 0) {
            this.statusBarItem.text = `$(brain) NT: ${this.stagingCount}`;
            this.statusBarItem.tooltip = `${this.stagingCount} insight(s) in staging. Click to review.`;
            this.statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        } else {
            this.statusBarItem.text = '$(brain) Neural Tape';
            this.statusBarItem.tooltip = 'Neural Tape — Click to open review panel';
            this.statusBarItem.backgroundColor = undefined;
        }
    }

    dispose(): void {
        this.statusBarItem.dispose();
    }
}
```

### 4.4 Pre-Load Command (src/commands/preLoad.ts)

```typescript
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as yaml from 'js-yaml';

interface Insight {
    type: string;
    title?: string;
    content: string;
    timestamp: string;
    confidence: string;
    project: string;
}

export class PreLoadCommand {
    private globalPath: string;
    private workspacePath: string;

    constructor(globalPath: string, workspacePath: string) {
        this.globalPath = globalPath;
        this.workspacePath = workspacePath;
    }

    async execute(): Promise<string> {
        const project = path.basename(this.workspacePath);
        const archiveDir = path.join(this.globalPath, 'tape', 'archive');

        // 1. Read archive
        const insights = await this.readArchive(archiveDir, project);

        // 2. Select relevant (recency + decay)
        const selected = this.selectRelevant(insights);

        // 3. Detect patterns
        const patterns = this.detectPatterns(insights);

        // 4. Check Deja Vu for recent staging
        const alerts = await this.checkRecentDejaVu();

        // 5. Generate session-context.md
        const contextPath = await this.generateContext(selected, patterns, alerts, project);

        // 6. Copy to clipboard or show notification
        const content = fs.readFileSync(contextPath, 'utf-8');
        await vscode.env.clipboard.writeText(content);

        return `Context generated: ${path.basename(contextPath)} (${selected.length} insights, ${patterns.length} patterns)`;
    }

    private async readArchive(archiveDir: string, project: string): Promise<Insight[]> {
        const insights: Insight[] = [];
        const categories = ['bug_found', 'eureka', 'warning', 'code_change'];

        for (const cat of categories) {
            const catDir = path.join(archiveDir, cat);
            if (!fs.existsSync(catDir)) continue;

            const files = fs.readdirSync(catDir).filter(f => f.endsWith('.md'));
            for (const file of files) {
                const content = fs.readFileSync(path.join(catDir, file), 'utf-8');
                const parsed = this.parseInsightFile(content);
                if (parsed && (parsed.project === project || !parsed.project)) {
                    insights.push(parsed);
                }
            }
        }

        return insights;
    }

    private parseInsightFile(content: string): Insight | null {
        const match = content.match(/^---
([\s\S]*?)
---/);
        if (!match) return null;

        try {
            const frontmatter = yaml.load(match[1]) as any;
            return {
                type: frontmatter.type || 'unknown',
                title: frontmatter.title,
                content: content.split('---').pop() || '',
                timestamp: frontmatter.timestamp,
                confidence: frontmatter.confidence || 'medium',
                project: frontmatter.project || ''
            };
        } catch {
            return null;
        }
    }

    private selectRelevant(insights: Insight[]): Insight[] {
        const config = vscode.workspace.getConfiguration('neuralTape');
        const maxInsights = config.get('preLoad.maxInsights', 10);
        const lookbackDays = 7;
        const cutoff = Date.now() - lookbackDays * 24 * 60 * 60 * 1000;

        return insights
            .filter(i => new Date(i.timestamp).getTime() > cutoff)
            .sort((a, b) => {
                // Score: recency (70%) + confidence (30%)
                const scoreA = this.scoreInsight(a);
                const scoreB = this.scoreInsight(b);
                return scoreB - scoreA;
            })
            .slice(0, maxInsights);
    }

    private scoreInsight(insight: Insight): number {
        const ageMs = Date.now() - new Date(insight.timestamp).getTime();
        const ageDays = ageMs / (1000 * 60 * 60 * 24);
        const recency = Math.exp(-ageDays / 7); // 7-day half-life

        const confidenceWeight = { high: 1.0, medium: 0.7, low: 0.4 };
        const conf = confidenceWeight[insight.confidence as keyof typeof confidenceWeight] || 0.5;

        return recency * 0.7 + conf * 0.3;
    }

    private detectPatterns(insights: Insight[]): any[] {
        // Group by normalized content similarity
        const groups: Map<string, Insight[]> = new Map();

        for (const insight of insights) {
            const normalized = this.normalizeForPattern(insight.content);
            const key = normalized.slice(0, 50);

            if (!groups.has(key)) groups.set(key, []);
            groups.get(key)!.push(insight);
        }

        return Array.from(groups.entries())
            .filter(([_, items]) => items.length >= 2)
            .map(([key, items]) => ({
                name: key,
                count: items.length,
                firstSeen: items[0].timestamp,
                lastSeen: items[items.length - 1].timestamp
            }))
            .slice(0, 5);
    }

    private normalizeForPattern(content: string): string {
        return content
            .toLowerCase()
            .replace(/[^a-z0-9\s]/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    }

    private async checkRecentDejaVu(): Promise<any[]> {
        // Check staging vs archive
        const stagingDir = path.join(this.workspacePath, '.vscode', 'neural-tape', 'tape', 'staging');
        if (!fs.existsSync(stagingDir)) return [];

        // Simplified: return empty for MVP
        return [];
    }

    private async generateContext(
        insights: Insight[],
        patterns: any[],
        alerts: any[],
        project: string
    ): Promise<string> {
        const contextDir = path.join(this.workspacePath, '.vscode', 'neural-tape');
        fs.mkdirSync(contextDir, { recursive: true });

        const contextPath = path.join(contextDir, 'session-context.md');
        const now = new Date().toISOString();
        const expiry = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();

        const content = `---
generated: ${now}
project: ${project}
source: neural-tape
expires: ${expiry}
---

# Session Context — Neural Tape

## Active Insights (${insights.length})
| Date | Type | Content | Confidence |
|------|------|---------|------------|
${insights.map(i => `| ${i.timestamp.slice(0, 10)} | ${i.type} | ${(i.title || i.content).slice(0, 50)}... | ${i.confidence} |`).join('\n')}

## Recurring Patterns (${patterns.length})
| Pattern | Count | First Seen | Last Seen |
|---------|-------|------------|-----------|
${patterns.map(p => `| ${p.name.slice(0, 30)} | ${p.count} | ${p.firstSeen.slice(0, 10)} | ${p.lastSeen.slice(0, 10)} |`).join('\n')}

## Deja Vu Alerts
${alerts.length > 0 ? alerts.map(a => `- ${a.similarity}% similar: ${a.preview}`).join('\n') : '_No alerts_'}

---
*Context generated by Neural Tape v2.1. Expires: ${expiry.slice(0, 10)}*
`;

        fs.writeFileSync(contextPath, content, 'utf-8');
        return contextPath;
    }
}
```

### 4.5 Post-Capture Command (src/commands/postCapture.ts)

```typescript
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

interface StagingInsight {
    id: string;
    filename: string;
    type: string;
    title: string;
    content: string;
    timestamp: string;
    confidence: string;
}

export class PostCaptureCommand {
    private globalPath: string;
    private workspacePath: string;

    constructor(globalPath: string, workspacePath: string) {
        this.globalPath = globalPath;
        this.workspacePath = workspacePath;
    }

    async execute(): Promise<void> {
        const insights = await this.readStaging();

        if (insights.length === 0) {
            vscode.window.showInformationMessage('Neural Tape: No insights to review');
            return;
        }

        // Show summary first
        const action = await vscode.window.showQuickPick(
            [
                { label: `$(check) Promote All (${insights.length})`, description: 'Move all to archive', action: 'promote-all' },
                { label: `$(eye) Review One by One`, description: 'Inspect each insight', action: 'review' },
                { label: `$(trash) Skip All`, description: 'Delete all from staging', action: 'skip-all' }
            ],
            { placeHolder: `Neural Tape: ${insights.length} insight(s) in staging` }
        );

        if (!action) return;

        switch (action.action) {
            case 'promote-all':
                await this.promoteAll(insights);
                break;
            case 'review':
                await this.reviewOneByOne(insights);
                break;
            case 'skip-all':
                await this.skipAll(insights);
                break;
        }
    }

    private async readStaging(): Promise<StagingInsight[]> {
        const stagingDir = path.join(this.workspacePath, '.vscode', 'neural-tape', 'tape', 'staging');
        if (!fs.existsSync(stagingDir)) return [];

        const files = fs.readdirSync(stagingDir).filter(f => f.endsWith('.md'));
        const insights: StagingInsight[] = [];

        for (const file of files) {
            const content = fs.readFileSync(path.join(stagingDir, file), 'utf-8');
            const parsed = this.parseStagingFile(content, file);
            if (parsed) insights.push(parsed);
        }

        return insights;
    }

    private parseStagingFile(content: string, filename: string): StagingInsight | null {
        // Extract type from filename: 2026-07-07-12345678-bug_found-title.md
        const typeMatch = filename.match(/-(bug_found|eureka|warning|code_change|meta)-/);
        const type = typeMatch ? typeMatch[1] : 'unknown';

        // Extract title from content (first heading)
        const titleMatch = content.match(/^# (.+)$/m);
        const title = titleMatch ? titleMatch[1] : filename;

        return {
            id: filename.replace('.md', ''),
            filename,
            type,
            title,
            content,
            timestamp: new Date().toISOString(),
            confidence: 'medium'
        };
    }

    private async promoteAll(insights: StagingInsight[]): Promise<void> {
        for (const insight of insights) {
            await this.promote(insight);
        }
        vscode.window.showInformationMessage(
            `Neural Tape: ${insights.length} insight(s) promoted to archive`
        );
    }

    private async reviewOneByOne(insights: StagingInsight[]): Promise<void> {
        for (let i = 0; i < insights.length; i++) {
            const insight = insights[i];

            const action = await vscode.window.showQuickPick(
                [
                    { 
                        label: '$(arrow-up) Promote', 
                        description: insight.title.slice(0, 80),
                        detail: `[${insight.type}] ${insight.content.slice(0, 200)}...`,
                        action: 'promote'
                    },
                    { label: '$(edit) Modify', action: 'modify' },
                    { label: '$(trash) Skip', action: 'skip' },
                    { label: '$(debug-step-over) Next', action: 'next' }
                ],
                { 
                    placeHolder: `Review ${i + 1}/${insights.length}: [${insight.type}]`,
                    ignoreFocusOut: true
                }
            );

            if (!action || action.action === 'next') continue;

            switch (action.action) {
                case 'promote':
                    await this.promote(insight);
                    break;
                case 'modify':
                    await this.modifyAndPromote(insight);
                    break;
                case 'skip':
                    await this.skip(insight);
                    break;
            }
        }
    }

    private async skipAll(insights: StagingInsight[]): Promise<void> {
        const stagingDir = path.join(this.workspacePath, '.vscode', 'neural-tape', 'tape', 'staging');
        for (const insight of insights) {
            fs.unlinkSync(path.join(stagingDir, insight.filename));
        }
        vscode.window.showInformationMessage('Neural Tape: All insights skipped');
    }

    private async promote(insight: StagingInsight): Promise<void> {
        const stagingDir = path.join(this.workspacePath, '.vscode', 'neural-tape', 'tape', 'staging');
        const archiveDir = path.join(this.globalPath, 'tape', 'archive', insight.type);

        fs.mkdirSync(archiveDir, { recursive: true });

        const archiveFilename = `${insight.id}.md`;
        const sourcePath = path.join(stagingDir, insight.filename);
        const destPath = path.join(archiveDir, archiveFilename);

        // Read and update status
        let content = fs.readFileSync(sourcePath, 'utf-8');
        content = content.replace('status: staging', 'status: verified');
        content += `\n\n## Promotion\n- Promoted: ${new Date().toISOString()}\n- By: VS Code Extension\n`;

        fs.writeFileSync(destPath, content, 'utf-8');
        fs.unlinkSync(sourcePath);
    }

    private async modifyAndPromote(insight: StagingInsight): Promise<void> {
        // Open in editor for modification
        const stagingDir = path.join(this.workspacePath, '.vscode', 'neural-tape', 'tape', 'staging');
        const uri = vscode.Uri.file(path.join(stagingDir, insight.filename));

        const doc = await vscode.workspace.openTextDocument(uri);
        await vscode.window.showTextDocument(doc);

        vscode.window.showInformationMessage(
            'Edit the file, then run "Neural Tape: Review Session" again to promote',
            'OK'
        );
    }

    private async skip(insight: StagingInsight): Promise<void> {
        const stagingDir = path.join(this.workspacePath, '.vscode', 'neural-tape', 'tape', 'staging');
        fs.unlinkSync(path.join(stagingDir, insight.filename));
    }
}
```

### 4.6 Chat Participant: @neural-tape (src/participant/neuralTapeParticipant.ts)

```typescript
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

/**
 * Neural Tape Chat Participant
 * 
 * When user types "@neural-tape <task>", this participant:
 * 1. Loads session context from archive
 * 2. Forwards the task to the selected language model
 * 3. Captures the full conversation (prompt, response, tool calls)
 * 4. Saves to staging
 * 
 * This is OPT-IN explicit capture — the user must call @neural-tape
 */
export class NeuralTapeParticipant {
    private globalPath: string;
    private workspacePath: string;
    private participant?: vscode.ChatParticipant;

    constructor(globalPath: string, workspacePath: string) {
        this.globalPath = globalPath;
        this.workspacePath = workspacePath;
    }

    register(context: vscode.ExtensionContext): void {
        this.participant = vscode.chat.createChatParticipant('neural-tape', {
            onRequest: async (request, context, response, token) => {
                await this.handleRequest(request, context, response, token);
            }
        });

        this.participant.iconPath = vscode.Uri.file(
            path.join(context.extensionPath, 'media', 'icon.svg')
        );

        // Register participant commands
        this.participant.commandProvider = {
            provideCommands: () => [
                {
                    name: 'endSession',
                    description: 'End Neural Tape session and review insights'
                },
                {
                    name: 'reviewStaging',
                    description: 'Review captured insights'
                }
            ]
        };
    }

    private async handleRequest(
        request: vscode.ChatRequest,
        context: vscode.ChatContext,
        response: vscode.ChatResponseStream,
        token: vscode.CancellationToken
    ): Promise<void> {
        const startTime = Date.now();

        // 1. Capture the user's prompt
        const capturedPrompt = request.prompt;
        const model = request.model;

        // 2. Load session context from archive
        const sessionContext = await this.loadSessionContext();

        // 3. Build enriched prompt with context
        const enrichedPrompt = this.buildEnrichedPrompt(capturedPrompt, sessionContext);

        // 4. Forward to the selected model
        response.markdown('🧠 *Neural Tape is capturing this session...*\n\n');

        const messages = [
            vscode.LanguageModelChatMessage.system(
                'You are an AI coding assistant. The following context contains relevant insights from previous sessions.'
            ),
            vscode.LanguageModelChatMessage.user(enrichedPrompt)
        ];

        // 5. Stream the response
        const modelResult = await model.sendRequest(messages, {}, token);
        let fullResponse = '';

        for await (const fragment of modelResult.text) {
            fullResponse += fragment;
            response.markdown(fragment);
        }

        // 6. Capture tool calls if any
        const toolCalls = modelResult.toolCalls || [];

        // 7. Save to staging
        const duration = Date.now() - startTime;
        await this.saveToStaging({
            prompt: capturedPrompt,
            response: fullResponse,
            model: model.name,
            vendor: model.vendor,
            toolCalls: toolCalls.map(tc => ({
                name: tc.name,
                parameters: tc.parameters
            })),
            duration: duration,
            timestamp: new Date().toISOString()
        });

        // 8. Update status bar
        response.markdown('\n\n---\n✅ *Session captured to Neural Tape staging*');
    }

    private async loadSessionContext(): Promise<string> {
        const contextPath = path.join(this.workspacePath, '.vscode', 'neural-tape', 'session-context.md');
        if (fs.existsSync(contextPath)) {
            return fs.readFileSync(contextPath, 'utf-8');
        }
        return '';
    }

    private buildEnrichedPrompt(userPrompt: string, context: string): string {
        if (!context) return userPrompt;

        return `${context}\n\n---\n\n${userPrompt}`;
    }

    private async saveToStaging(data: {
        prompt: string;
        response: string;
        model: string;
        vendor: string;
        toolCalls: any[];
        duration: number;
        timestamp: string;
    }): Promise<void> {
        const stagingDir = path.join(this.workspacePath, '.vscode', 'neural-tape', 'tape', 'staging');
        fs.mkdirSync(stagingDir, { recursive: true });

        const sessionId = this.generateSessionId();
        const filename = `${data.timestamp.replace(/[:.]/g, '-')}-${sessionId}-eureka-neural-tape-session.md`;
        const filepath = path.join(stagingDir, filename);

        const content = `---
type: eureka
session_id: ${sessionId}
project: ${path.basename(this.workspacePath)}
timestamp: ${data.timestamp}
confidence: high
trigger: neural-tape-participant
source: participant
status: staging
model: ${data.model}
provider: ${data.vendor}
duration_ms: ${data.duration}
tool_calls_count: ${data.toolCalls.length}
---

# EUREKA — Neural Tape Session

## Prompt
${data.prompt}

## Response
${data.response}

## Tool Calls
${data.toolCalls.length > 0 
    ? data.toolCalls.map(tc => `- **${tc.name}**: ${JSON.stringify(tc.parameters)}`).join('\n')
    : '_No tool calls_'
}

## Metadata
- Model: ${data.model} (${data.vendor})
- Duration: ${data.duration}ms
- Tool calls: ${data.toolCalls.length}

## Resolution (add on promotion)
- Fix:
- Verified:
- By:
`;

        fs.writeFileSync(filepath, content, 'utf-8');
    }

    private generateSessionId(): string {
        return Math.random().toString(36).substring(2, 10);
    }

    dispose(): void {
        this.participant?.dispose();
    }
}
```

### 4.7 Deja Vu Checker (src/intelligence/dejaVu.ts)

```typescript
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

export interface DejaVuAlert {
    similarity: number;
    reference: string;
    preview: string;
    archivedFile: string;
}

export class DejaVuChecker {
    private globalPath: string;
    private threshold: number;

    constructor(globalPath: string) {
        this.globalPath = globalPath;
        const config = vscode.workspace.getConfiguration('neuralTape');
        this.threshold = config.get('dejaVu.threshold', 0.75);
    }

    async checkStaging(workspacePath: string): Promise<DejaVuAlert[]> {
        const stagingDir = path.join(workspacePath, '.vscode', 'neural-tape', 'tape', 'staging');
        const archiveDir = path.join(this.globalPath, 'tape', 'archive');

        if (!fs.existsSync(stagingDir)) return [];

        const stagingFiles = fs.readdirSync(stagingDir).filter(f => f.endsWith('.md'));
        const alerts: DejaVuAlert[] = [];

        for (const stagingFile of stagingFiles) {
            const stagingContent = fs.readFileSync(path.join(stagingDir, stagingFile), 'utf-8');
            const stagingNormalized = this.normalize(stagingContent);

            // Check against archive
            const archiveInsights = await this.readArchive(archiveDir);

            for (const archive of archiveInsights) {
                const similarity = this.calculateSimilarity(
                    stagingNormalized,
                    this.normalize(archive.content)
                );

                if (similarity >= this.threshold) {
                    alerts.push({
                        similarity: Math.round(similarity * 100),
                        reference: archive.id,
                        preview: archive.title || archive.content.slice(0, 100),
                        archivedFile: archive.path
                    });
                }
            }
        }

        return alerts.sort((a, b) => b.similarity - a.similarity);
    }

    private async readArchive(archiveDir: string): Promise<any[]> {
        const insights: any[] = [];
        const categories = ['bug_found', 'eureka', 'warning', 'code_change'];

        for (const cat of categories) {
            const catDir = path.join(archiveDir, cat);
            if (!fs.existsSync(catDir)) continue;

            const files = fs.readdirSync(catDir).filter(f => f.endsWith('.md'));
            for (const file of files) {
                const content = fs.readFileSync(path.join(catDir, file), 'utf-8');
                const titleMatch = content.match(/^# (.+)$/m);
                insights.push({
                    id: file.replace('.md', ''),
                    title: titleMatch ? titleMatch[1] : file,
                    content,
                    path: path.join(catDir, file)
                });
            }
        }

        return insights;
    }

    private normalize(text: string): string {
        return text
            .replace(/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/g, 'SESSION_ID')
            .replace(/tool_[a-zA-Z0-9]{20,}/g, 'CALL_ID')
            .replace(/\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/g, 'DATETIME')
            .replace(/(?:\d{1,3}\.){3}\d{1,3}/g, 'IP')
            .replace(/\d+/g, 'NUM')
            .toLowerCase()
            .replace(/[^a-z0-9\s]/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    }

    private calculateSimilarity(a: string, b: string): number {
        // Jaccard similarity
        const tokensA = new Set(a.split(/\s+/));
        const tokensB = new Set(b.split(/\s+/));

        const intersection = new Set([...tokensA].filter(x => tokensB.has(x)));
        const union = new Set([...tokensA, ...tokensB]);

        if (union.size === 0) return 0;
        const jaccard = intersection.size / union.size;

        // Sequence similarity (simplified)
        const sequenceSim = this.sequenceSimilarity(a, b);

        return jaccard * 0.6 + sequenceSim * 0.4;
    }

    private sequenceSimilarity(a: string, b: string): number {
        const maxLen = Math.max(a.length, b.length);
        if (maxLen === 0) return 1.0;

        const distance = this.levenshtein(a, b);
        return 1 - distance / maxLen;
    }

    private levenshtein(a: string, b: string): number {
        const matrix: number[][] = [];
        for (let i = 0; i <= b.length; i++) matrix[i] = [i];
        for (let j = 0; j <= a.length; j++) matrix[0][j] = j;

        for (let i = 1; i <= b.length; i++) {
            for (let j = 1; j <= a.length; j++) {
                matrix[i][j] = b.charAt(i - 1) === a.charAt(j - 1)
                    ? matrix[i - 1][j - 1]
                    : Math.min(
                        matrix[i - 1][j - 1] + 1,
                        matrix[i][j - 1] + 1,
                        matrix[i - 1][j] + 1
                    );
            }
        }

        return matrix[b.length][a.length];
    }
}
```

### 4.8 Review Panel (src/ui/reviewPanel.ts) — MVP QuickPick

```typescript
import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

export class ReviewPanel {
    private globalPath: string;
    private workspacePath: string;

    constructor(globalPath: string, workspacePath: string) {
        this.globalPath = globalPath;
        this.workspacePath = workspacePath;
    }

    async show(): Promise<void> {
        // For MVP, delegate to PostCaptureCommand (QuickPick)
        const { PostCaptureCommand } = await import('../commands/postCapture');
        const cmd = new PostCaptureCommand(this.globalPath, this.workspacePath);
        await cmd.execute();
    }
}
```

---

## 5. Implementation Plan

### Phase 1: MVP — UI + Storage (3-4 giorni)

| Task | Effort | Deliverable |
|------|--------|-------------|
| 1.1 Project scaffold | 2h | neural-tape-vscode/ con TypeScript |
| 1.2 package.json + manifest | 1h | Manifest completo con chat participant |
| 1.3 StatusBar | 2h | Icona + contatore staging |
| 1.4 PreLoadCommand | 4h | Legge archive, genera session-context.md |
| 1.5 PostCaptureCommand (QuickPick) | 4h | Review promote/skip/modify |
| 1.6 DejaVuChecker | 3h | Similarity detection, popup alert |
| 1.7 Storage compatibility test | 2h | Verifica formato v1.2 |
| **Milestone** | | **Status bar, pre-load, review, Deja Vu funzionanti** |

### Phase 2: Chat Participant (2-3 giorni)

| Task | Effort | Deliverable |
|------|--------|-------------|
| 2.1 NeuralTapeParticipant | 4h | @neural-tape registra, cattura, salva |
| 2.2 Context enrichment | 2h | Carica session-context.md, inietta nel prompt |
| 2.3 Tool call capture | 2h | Cattura tool calls dal model result |
| 2.4 Participant commands | 2h | /endSession, /reviewStaging |
| **Milestone** | | **@neural-tape funzionante in chat** |

### Phase 3: Polish + Integration (2-3 giorni)

| Task | Effort | Deliverable |
|------|--------|-------------|
| 3.1 Auto-preload on workspace open | 2h | Event-driven activation |
| 3.2 Channel selection UI | 2h | QuickPick per scegliere canale |
| 3.3 Webview review panel (futuro) | 4h | UI completa (post-MVP) |
| 3.4 Testing + bugfix | 3h | Unit test, integration |
| 3.5 README + docs | 2h | Documentazione |
| **Milestone** | | **v2.1 pronto per uso** |

**Totale stimato: 7-10 giorni**

---

## 6. Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Chat Participant API cambia in futuro | Medium | High | API e stabile da VS Code 1.90+, wrapper per astrazione |
| Deduplicazione tra log-parser Python e participant | Medium | Medium | SHA-256 su contenuto normalizzato, TTL 5 min |
| Performance con archive grande | Low | Medium | Lazy loading, cache in memoria, max 100 insight |
| Compatibilita formato v1.2 | Low | High | Stesso formato YAML frontmatter + Markdown |
| Utente dimentica di usare @neural-tape | High | Medium | Status bar reminder, canale ibrido default |

---

## 7. Future Roadmap

### v2.2 (Month 2)
- Webview review panel completo (tabella sortable, filtri)
- Semantic search su archive (TF-IDF)
- Auto-promotion dopo N occorrenze
- Metrics: "Hai evitato X bug questa settimana"

### v2.3 (Month 3)
- Terminal capture via Shell Integration API
- Export diretto a EterCervo wiki
- Multi-workspace sync

### v3.0 (Month 6)
- Shared memory cross-device (Git-based)
- Community patterns
- HERMES integration

---

## 8. Appendix

### A.1 Compatibilita v1.2

| Componente v1.2 | Equivalente v2.1 | Compatibilita |
|-----------------|------------------|---------------|
| log-parser.py | Rimane invariato | ✅ 100% |
| pre-load.py | PreLoadCommand | ✅ Stesso formato output |
| deja-vu.py | DejaVuChecker | ✅ Stesso algoritmo |
| post-capture.py | PostCaptureCommand | ✅ Stesso flusso review |
| config.yaml | package.json configuration | ⚠️ Diversa UI, stessi valori |
| tape/staging/*.md | Stesso formato | ✅ Identico |
| tape/archive/*.md | Stesso formato | ✅ Identico |
| session-context.md | Stesso formato | ✅ Identico |

### A.2 Directory Structure

```
neural-tape-vscode/           # Extension source
├── package.json
├── tsconfig.json
├── src/
│   ├── extension.ts          # Entry point
│   ├── commands/
│   │   ├── preLoad.ts
│   │   └── postCapture.ts
│   ├── participant/
│   │   └── neuralTapeParticipant.ts
│   ├── intelligence/
│   │   └── dejaVu.ts
│   ├── ui/
│   │   ├── statusBar.ts
│   │   └── reviewPanel.ts
│   └── storage/
│       └── stagingManager.ts
├── media/
│   └── icon.svg
└── out/                      # Compiled JS

Runtime storage:
Workspace: .vscode/neural-tape/
  ├── tape/staging/           # Sessione corrente
  ├── tape/sessions/          # Raw (opzionale)
  └── session-context.md      # Context generato

Global: ~/.neural-tape/
  ├── tape/archive/
  │   ├── bug_found/
  │   ├── eureka/
  │   ├── warning/
  │   └── code_change/
  └── index.md                # Catalogo
```

### A.3 agents.md Update (per Lex)

```markdown
## Neural Tape Channel Selection

All'inizio di ogni sessione, dopo il pre-load:

1. Chiedi all'utente: "Canale di cattura per questa sessione?"
   - [A] Terminale/Kimi Code — log parser Python (default)
   - [B] VS Code Chat — @neural-tape participant (cattura esplicita)
   - [C] Ibrido — entrambi (massima copertura)

2. Se l'utente sceglie B o C:
   - Ricorda: "Per attivare la cattura, scrivi @neural-tape <task> nella chat"
   - Nota: La cattura inizia SOLO quando invochi @neural-tape

3. Se l'utente sceglie A o C:
   - Avvia log parser Python come sempre
   - Ricorda: end-sessions.sh a fine sessione

4. Salva la preferenza in lex-state.json per future sessioni
```

### A.4 Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-07 | Log parser Python rimane invariato | Funziona, non rompere ciò che funziona |
| 2026-07-07 | Extension = UI + Intelligence layer | API VS Code non permette cattura passiva |
| 2026-07-07 | @neural-tape participant come opt-in | Cattura esplicita, API realmente esistente |
| 2026-07-07 | QuickPick per review MVP | Webview come fase 2, QuickPick e sufficiente |
| 2026-07-08 | Channel selection (A/B/C) | L'utente sceglie il canale, Lex non puo decidere per lui |
| 2026-07-08 | Hybrid come default | Massima copertura, fallback automatico |
