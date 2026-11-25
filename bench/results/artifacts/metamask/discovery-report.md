# Journey exploration — metamask

Explorer: agent-submitted discovery. Evidence verified against the repo: 42 of 42 citations (file_only 10, verified 32).

Confidence is computed (grounded steps + independent e2e/analytics/docs corroboration), never taken from the model. **business_impact is unset on every journey.**

| # | Journey | Confidence | Steps | Corroborated by |
|---|---|---|---|---|
| 1 | Create or import a wallet (`wallet_onboarding`) | high | 4 | e2e, analytics |
| 2 | Unlock the wallet (`unlock_wallet`) | high | 3 | e2e |
| 3 | Review and confirm a transaction or signature (`confirm_transaction_or_signature`) | high | 3 | e2e, analytics |
| 4 | Swap or bridge assets (`swap_or_bridge`) | high | 3 | e2e, analytics, docs |
| 5 | Connect a dapp (`connect_dapp`) | high | 3 | e2e |
| 6 | Add and switch networks (`add_network`) | high | 2 | e2e, analytics |
| 7 | Import tokens and NFTs (`import_token_or_nft`) | high | 2 | e2e, analytics |
| 8 | Subscribe to Shield (`subscribe_shield`) | high | 2 | e2e |
| 9 | Send tokens or NFTs (`send_assets`) | medium | 4 | e2e |
| 10 | Add accounts and export keys (`manage_accounts`) | medium | 2 | e2e |
| 11 | Sign with a hardware wallet (`hardware_wallet_signing`) | medium | 3 | e2e, analytics |
| 12 | Trade perpetuals (`perps_trading`) | medium | 3 | e2e |

## Create or import a wallet
Establishes the vault that holds the user's keys; failure here blocks a new user entirely.

1. **set_password** — User sets a password and accepts terms (`ui/pages/onboarding-flow/create-password/create-password.tsx:368`, `ui/pages/onboarding-flow/create-password/create-password.tsx:256`)
2. **create_vault** — New vault and recovery phrase created (social path syncs to the seedless service) (`ui/pages/onboarding-flow/onboarding-flow.tsx:257`, `ui/store/actions.ts:1196`)
3. **restore_vault** — Import path restores a vault from a recovery phrase (`ui/pages/onboarding-flow/onboarding-flow.tsx:360`, `ui/store/actions.ts:1149`)
4. **complete_onboarding** — Onboarding marked complete (`ui/pages/onboarding-flow/onboarding-flow.tsx:328`, `ui/pages/onboarding-flow/onboarding-flow.tsx:343`)
- ❓ Recovery phrase confirmation and metrics-consent steps were not traced
- ❓ Seedless/social login path only partly read

## Unlock the wallet
Every session begins here; failures lock users out of funds.

1. **submit_password** — Password form submitted (`ui/pages/unlock-page/unlock-page.component.tsx:300`)
2. **verify_password** — Background verifies and decrypts the vault (`ui/store/actions.ts:1241`)
3. **reset_password** — Forgot-password path (`ui/pages/unlock-page/reset-password-modal.tsx`)
- ❓ Passkey unlock path was not read

## Review and confirm a transaction or signature
The consent point for every on-chain action and dapp signature.

1. **render_confirmation** — Confirmation screen shown with alerts (`ui/pages/confirmations/confirm/confirm.tsx`)
2. **confirm** — User confirms; transaction confirm hook runs (`ui/pages/confirmations/components/confirm/footer/footer.tsx:323`, `ui/pages/confirmations/components/confirm/footer/footer.tsx:168`)
3. **cancel** — User rejects or cancels (`ui/pages/confirmations/hooks/useConfirmActions.ts:50`)

## Swap or bridge assets
Revenue-generating, multi-step cross-chain flow with quotes and signatures.

1. **request_quotes** — Quote request parameters updated as the user edits (`ui/pages/bridge/prepare/prepare-bridge-page.tsx:376`)
2. **submit_cta** — User submits the active quote (`ui/pages/bridge/prepare/bridge-cta-button.tsx:162`, `ui/pages/bridge/prepare/bridge-cta-button.tsx:59`)
3. **submit_quote** — Quote turned into transaction(s) and submitted (`ui/hooks/bridge/useSubmitBridgeTransaction.ts:126`, `ui/hooks/bridge/useSubmitBridgeTransaction.ts:228`)
- ❓ Legacy swaps page (ui/pages/swaps) not traced

## Connect a dapp
Gate for every dapp interaction; permission bugs expose accounts.

1. **review_request** — User reviews the requested accounts and chains (`ui/pages/permissions-connect/permissions-connect.tsx:517`)
2. **approve_permissions** — Permissions granted (`ui/pages/permissions-connect/permissions-connect.tsx:485`)
3. **reject_permissions** — Request rejected (`ui/pages/permissions-connect/permissions-connect.tsx:474`)
- ❓ Background permission controller not read

## Add and switch networks
Wrong network config sends funds to the wrong chain.

1. **add_network** — Popular network added (`ui/components/multichain/network-list-menu/popular-network-list/popular-network-list.tsx:182`)
2. **switch_network** — Active network switched (`ui/components/multichain/network-list-menu/network-list-menu.tsx:477`)
- ❓ Custom RPC add form not traced

## Import tokens and NFTs
Asset visibility; spoofed or failed imports mislead users about balances.

1. **import_token** — Custom token imported (`ui/pages/custom-token-import/custom-token-import.tsx:499`)
2. **import_nft** — NFT imported after verifying ownership (`ui/components/multichain/import-nfts-modal/import-nfts-form.tsx:159`)

## Subscribe to Shield
Paid subscription checkout; failures lose revenue and strand paying users.

1. **start_checkout** — Subscription started with card through a hosted checkout session (`app/scripts/services/subscription/shield-subscription-service.ts:174`)
2. **controller_call** — Controller creates the checkout session (`app/scripts/services/subscription/shield-subscription-service.ts:190`)
- ❓ Crypto payment path and polling not read

## Send tokens or NFTs
Moves user funds; address poisoning or amount bugs cause irreversible loss.

1. **validate_amount** — Amount validated against balance and gas (`ui/pages/confirmations/hooks/send/useAmountValidation.ts`)
2. **check_recipient** — Recipient checked for address poisoning (`ui/pages/confirmations/hooks/send/useAddressPoisoningDetection.ts`)
3. **submit_send** — Send submitted, then routed to loader and confirmation (`ui/pages/confirmations/hooks/send/useSendActions.ts:61`, `ui/pages/confirmations/hooks/send/useSendActions.ts:103`)
4. **land_on_activity** — User lands on activity after submit (`ui/pages/confirmations/hooks/send/useSendActions.ts:141`)
- ❓ Bitcoin and Tron send variants not traced

## Add accounts and export keys
Account creation and private-key export are high-risk identity operations.

1. **import_account** — Account imported by strategy (`ui/store/actions.ts:1346`)
2. **export_private_key** — Private keys revealed or exported (`ui/components/multichain-accounts/multichain-private-key-list/multichain-private-key-list.tsx`)
- ❓ Multichain account group creation not traced

## Sign with a hardware wallet
Device disconnects and rejections strand in-flight transactions.

1. **sign_tracking** — Signing progress tracked for batches (`ui/hooks/hardware-wallets/hw-sign-tracker/batch-tracking-strategy.ts`)
2. **swap_actions** — Swap actions on a hardware device (`ui/hooks/hardware-wallets/useHwSwapActions.ts`)
3. **device_events** — Device connect/disconnect events handled (`ui/contexts/hardware-wallets/HardwareWalletEventHandlers.tsx`)
- ❓ Entry-to-effect path not traced; evidence is file-level only

## Trade perpetuals
Leveraged trading; order and close failures cost users money directly.

1. **perps_view** — User views positions and orders (`ui/components/app/perps/perps-view.tsx`)
2. **close_position** — Close-position modal (`ui/components/app/perps/close-position/close-position-modal.tsx`)
3. **controller_infra** — Perps controller infrastructure handles orders and withdrawals (`app/scripts/controllers/perps/infrastructure.ts:121`)
- ❓ Order placement path not traced; evidence is file-level for two steps
