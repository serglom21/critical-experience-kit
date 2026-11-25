# Journey exploration — supabase

Explorer: agent-submitted discovery. Evidence verified against the repo: 52 of 53 citations (file_only 3, missing_file 1, verified 49).

Confidence is computed (grounded steps + independent e2e/analytics/docs corroboration), never taken from the model. **business_impact is unset on every journey.**

| # | Journey | Confidence | Steps | Corroborated by |
|---|---|---|---|---|
| 1 | Sign up and sign in (`sign_up_and_sign_in`) | high | 4 | analytics |
| 2 | Create project (`create_project`) | high | 4 | analytics |
| 3 | Change subscription plan (`change_subscription_plan`) | high | 3 | analytics |
| 4 | Manage project auth users (`manage_auth_users`) | high | 3 | e2e, analytics |
| 5 | Create and edit tables and rows (`edit_table_data`) | high | 4 | e2e, analytics |
| 6 | Run SQL in the SQL editor (`run_sql_query`) | high | 3 | e2e, analytics |
| 7 | Create buckets and upload files (`storage_buckets_and_files`) | high | 3 | e2e, analytics |
| 8 | Deploy and manage edge functions (`deploy_edge_function`) | high | 4 | e2e, analytics |
| 9 | Create and merge database branches (`database_branching`) | high | 3 | e2e, analytics |
| 10 | Enable RLS and manage access policies (`rls_policies`) | high | 3 | e2e, analytics |
| 11 | Schedule and delete cron jobs (`schedule_cron_job`) | high | 2 | e2e |
| 12 | Create and revoke access tokens (`create_access_token`) | high | 2 | analytics |
| 13 | Create organization (with payment) (`create_organization`) | medium | 3 | code only |
| 14 | Accept organization invitation (`accept_organization_invite`) | medium | 3 | code only |

## Sign up and sign in
Nobody reaches the product without it; MFA and redirect failures lock users out.

1. **sign_up_submit** — User submits email and password to create an account (`apps/studio/components/interfaces/SignIn/SignUpForm.tsx:80`, `apps/studio/components/interfaces/SignIn/SignUpForm.tsx:66`)
2. **password_sign_in** — Credentials are verified against auth (`apps/studio/components/interfaces/SignIn/SignInForm.tsx:80`)
3. **mfa_challenge** — Accounts with MFA are redirected to the second-factor form (`apps/studio/components/interfaces/SignIn/SignInForm.tsx:93`, `apps/studio/components/interfaces/SignIn/SignInMfaForm.tsx`)
4. **redirect_into_app** — Session established, user routed to their destination (`apps/studio/components/interfaces/SignIn/SignInForm.tsx:109`)
- ❓ SSO and OAuth variants (SignInSSOForm, SignInWithExternalProvider) were not traced

## Create project
Provisions the customer's database; the core activation event.

1. **confirm_compute_costs** — Compute cost confirmation before submit (`apps/studio/components/interfaces/ProjectCreation/ProjectCreationForm.tsx:434`)
2. **build_payload** — Region, instance size, Postgres options assembled (`apps/studio/components/interfaces/ProjectCreation/ProjectCreationForm.tsx:456`)
3. **provision_project** — Project creation request sent (`apps/studio/components/interfaces/ProjectCreation/ProjectCreationForm.tsx:639`)
4. **track_success** — Success handler records the submission (`apps/studio/components/interfaces/ProjectCreation/ProjectCreationForm.tsx:383`)
- ❓ Project readiness polling after create was not traced

## Change subscription plan
Upgrades and downgrades move revenue; errors mis-bill customers.

1. **confirm_plan_update** — Dialog confirms and applies the subscription update (`apps/studio/components/interfaces/Organization/BillingSettings/Subscription/SubscriptionPlanUpdateDialog.tsx:227`, `apps/studio/components/interfaces/Organization/BillingSettings/Subscription/SubscriptionPlanUpdateDialog.tsx:152`)
2. **exit_survey** — Downgrades go through an exit survey (`apps/studio/components/interfaces/Organization/BillingSettings/Subscription/ExitSurveyModal.tsx:44`)
3. **spend_cap** — Spend cap toggled via the same subscription update (`apps/studio/components/interfaces/Organization/BillingSettings/CostControl/SpendCapSidePanel.tsx:98`)
- ❓ Invoice and payment-failure handling were not read

## Manage project auth users
Customers' end users are created, invited and deleted here.

1. **create_user** — Admin creates a user (`apps/studio/components/interfaces/Auth/Users/CreateUserModal.tsx:44`)
2. **invite_user** — Admin invites a user by email (`apps/studio/components/interfaces/Auth/Users/InviteUserModal.tsx:45`)
3. **delete_user** — Admin deletes a user (`apps/studio/components/interfaces/Auth/Users/DeleteUserModal.tsx:23`)

## Create and edit tables and rows
Core database editing; failed saves lose customer schema or data.

1. **save_table** — Side panel saves a table definition (`apps/studio/components/interfaces/TableGridEditor/SidePanelEditor/SidePanelEditor.tsx:586`)
2. **create_table** — Table created (`apps/studio/components/interfaces/TableGridEditor/SidePanelEditor/SidePanelEditor.tsx:666`)
3. **create_column** — Columns created or edited (`apps/studio/components/interfaces/TableGridEditor/SidePanelEditor/SidePanelEditor.tsx:410`)
4. **delete_rows** — Rows deleted after confirmation (`apps/studio/components/interfaces/TableGridEditor/DeleteConfirmationDialogs.tsx:117`)
- ❓ Row insert/edit path in the grid was not traced

## Run SQL in the SQL editor
Direct production database access; slow or failed runs are visible to customers.

1. **run_query** — User triggers a run (`apps/studio/components/interfaces/SQLEditor/SQLEditorLayout.tsx:74`)
2. **execute** — Guarded execution of the SQL (`apps/studio/components/interfaces/SQLEditor/SQLEditorLayout.tsx:83`, `apps/studio/components/interfaces/SQLEditor/useSqlEditorExecution.ts:77`)
3. **record_result** — Result stored in the session (`apps/studio/components/interfaces/SQLEditor/useSqlEditorExecution.ts:79`)

## Create buckets and upload files
Customer file storage; upload failures lose end-user data.

1. **create_bucket** — Bucket created (`apps/studio/components/interfaces/Storage/FilesBuckets/CreateBucketModal.tsx:120`, `apps/studio/components/interfaces/Storage/FilesBuckets/CreateBucketModal.tsx:78`)
2. **upload_files** — Files uploaded to a bucket (`apps/studio/components/interfaces/Storage/BucketFilePickerDialog/BucketFilePickerHeader.tsx:165`)
3. **delete_bucket_policies** — Deleting a bucket removes its policies (`apps/studio/components/interfaces/Storage/FilesBuckets/DeleteBucketModal.tsx:31`)
- ❓ Main storage explorer upload path was not traced

## Deploy and manage edge functions
Customer backend code; a failed deploy leaves stale or broken functions.

1. **start_deploy** — User chooses to deploy from the dashboard (`apps/studio/components/interfaces/EdgeFunctions/DeployEdgeFunctionButton.tsx`)
2. **deploy_request** — Deploy request sent to the platform API (`apps/studio/data/edge-functions/edge-functions-deploy-mutation.ts:40`, `apps/studio/data/edge-functions/edge-functions-deploy-mutation.ts:24`)
3. **update_function** — Function settings updated (`apps/studio/components/interfaces/Functions/EdgeFunctionDetails/EdgeFunctionDetails.tsx:113`)
4. **delete_function** — Function deleted (`apps/studio/components/interfaces/Functions/EdgeFunctionDetails/EdgeFunctionDetails.tsx:114`)

## Create and merge database branches
Preview environments; failed merges affect production schema.

1. **create_branch** — Branch created (`apps/studio/components/interfaces/BranchManagement/CreateBranchModal.tsx:189`)
2. **update_branch** — Branch updated (`apps/studio/components/interfaces/BranchManagement/Overview.tsx:306`)
3. **merge_request** — Merge request opened from the header (`apps/studio/components/layouts/Navigation/LayoutHeader/MergeRequestButton.tsx`)
- ❓ Merge execution path was not traced

## Enable RLS and manage access policies
Security boundary of customer data.

1. **enable_rls** — Table RLS toggled from the grid header (`apps/studio/components/interfaces/TableGridEditor/GridHeaderActions.tsx:107`)
2. **create_policy** — Policy created (storage policies shown) (`apps/studio/components/interfaces/Storage/StoragePolicies/StoragePolicies.tsx:76`)
3. **delete_policy** — Policy deleted (`apps/studio/components/interfaces/Database/Policies/Policies.tsx:68`)
- ❓ Main policy create/edit flow in Database/Policies was not read

## Schedule and delete cron jobs
Customer scheduled work.

1. **upsert_job** — Cron job created or updated (`apps/studio/components/interfaces/Integrations/CronJobs/CreateCronJobSheet/CreateCronJobSheet.tsx:117`)
2. **delete_job** — Cron job deleted (`apps/studio/components/interfaces/Integrations/CronJobs/DeleteCronJob.tsx:32`)

## Create and revoke access tokens
Credentials for the management API.

1. **create_token** — Token created (`apps/studio/components/interfaces/Account/AccessTokens/Classic/NewTokenDialog.tsx:72`)
2. **delete_token** — Token deleted (`apps/studio/components/interfaces/Account/AccessTokens/AccessTokenList.tsx:55`)

## Create organization (with payment)
Creates the billing entity; a failure here blocks a paying customer from starting.

1. **submit_form** — Form submitted; free plan skips payment (`apps/studio/components/interfaces/Organization/NewOrg/NewOrgForm.tsx:457`)
2. **create_payment_method** — Paid plans tokenize a payment method through Stripe before creating (`apps/studio/components/interfaces/Organization/NewOrg/NewOrgForm.tsx:466`, `apps/studio/components/interfaces/Organization/NewOrg/NewOrgForm.tsx:98`)
3. **create_organization** — Organization created with tier, payment method and billing details (`apps/studio/components/interfaces/Organization/NewOrg/NewOrgForm.tsx:445`)
- ❓ 3DS / next-action handling for PaymentIntent was not traced

## Accept organization invitation
How teammates join paying orgs; MFA-required orgs can strand invitees.

1. **check_mfa_required** — Invitation page checks whether MFA is required (`apps/studio/components/interfaces/OrganizationInvite/OrganizationInvite.tsx:71`)
2. **join_organization** — Invitee accepts with slug and token (`apps/studio/components/interfaces/OrganizationInvite/OrganizationInvite.tsx:83`, `apps/studio/components/interfaces/OrganizationInvite/OrganizationInvite.tsx:77`)
3. **redirect_to_organizations** — On success user lands on the organizations list (`apps/studio/components/interfaces/OrganizationInvite/OrganizationInvite.tsx:79`)
- ❓ Where invitations are sent was not traced
