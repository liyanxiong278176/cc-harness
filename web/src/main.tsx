import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './cc/app'
import './cc/deepseek-tokens.css'
import './styles.css'

// Keep one Vite entry point. The DeepSeek Harness source snapshot is vendored
// under `vendor/deepseek-ui`; this adapter owns cc-harness' Runtime contract
// while the App presents the same calm, three-column interaction model.
const root = document.getElementById('root')
if (!root) throw new Error('cc-harness WebUI: missing #root')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
