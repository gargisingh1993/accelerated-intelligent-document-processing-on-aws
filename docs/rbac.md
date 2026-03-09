# Role-Based Access Control (RBAC)

## Overview

The GenAI IDP Accelerator implements a comprehensive Role-Based Access Control system with **server-side enforcement** at the AppSync API layer, supplemented by UI-level navigation and action controls for a clean user experience.

## Roles

Four roles are defined as Cognito User Pool groups:

| Role | Cognito Group | Description |
|------|--------------|-------------|
| **Admin** | `Admin` | Full access to all operations including user management |
| **Author** | `Author` | Read + write access to documents, configuration, tests, discovery |
| **Reviewer** | `Reviewer` | HITL review operations + limited document visibility |
| **Viewer** | `Viewer` | Read-only access to documents, configuration, agent chat |

### Multi-Group Support

Users can belong to multiple groups. Permissions are the **union** of all group permissions. For example, a user in both `Author` and `Reviewer` groups can both write documents and perform HITL reviews.

## Permission Matrix

```
Feature / API                    Admin   Author   Reviewer   Viewer
──────────────────────────────────────────────────────────────────────
DOCUMENTS
  List documents                  ✅      ✅       ✅*       ✅
  View document details           ✅      ✅       ✅*       ✅
  Upload documents                ✅      ✅       ❌        ❌
  Delete documents                ✅      ✅       ❌        ❌
  Reprocess documents             ✅      ✅       ❌        ❌
  Abort workflows                 ✅      ✅       ❌        ❌

HITL REVIEW
  Claim/Release review            ✅      ❌       ✅        ❌
  Complete section review         ✅      ❌       ✅        ❌
  Skip all section reviews        ✅      ❌       ✅        ❌
  Process changes (edit mode)     ✅      ❌       ✅        ❌

CONFIGURATION
  View config versions            ✅      ✅       ❌        ✅
  Edit configuration              ✅      ✅       ❌        ❌
  Delete config version           ✅      ❌       ❌        ❌
  Set active version              ✅      ✅       ❌        ❌
  Sync BDA                        ✅      ✅       ❌        ❌

DISCOVERY
  List/run discovery jobs         ✅      ✅       ❌        ❌

AGENT CHAT & CODE EXPLORER
  Chat with agent                 ✅      ✅       ❌        ✅
  Code intelligence               ✅      ✅       ❌        ✅

TEST STUDIO
  View/run test sets              ✅      ✅       ❌        ❌
  Create/delete test sets         ✅      ✅       ❌        ❌

CAPACITY PLANNING
  Calculate capacity              ✅      ✅       ❌        ✅

USER MANAGEMENT
  List/create/delete users        ✅      ❌       ❌        ❌

PRICING
  View pricing                    ✅      ✅       ❌        ✅
  Edit pricing                    ✅      ✅       ❌        ❌

✅* = Reviewer sees only HITL-pending docs + their own completed reviews (server-side filtered)
```

## Enforcement Layers

### Layer 1: AppSync Schema Auth Directives (Server-Side — Mutations Only)

Every GraphQL **mutation** has `@aws_auth(cognito_groups: [...])` directives that enforce write access at the AppSync level. If a user's Cognito group is not in the allowed list, AppSync returns an **Unauthorized** error before any resolver code runs.

**Important**: `@aws_auth` directives are applied to **Mutations only**, not Queries. This is because AppSync's `@aws_auth` on individual Query fields overrides the type-level `@aws_cognito_user_pools` directive and conflicts with the `DefaultAction: ALLOW` configuration. Read access for Queries is controlled by:
- **UI navigation** (which features are visible per role)
- **Server-side resolver filtering** (e.g., reviewer document filtering in Lambda)

Example:
```graphql
# Only Admin can delete config versions
deleteConfigVersion(versionName: String!): UpdateConfigurationResponse
  @aws_auth(cognito_groups: ["Admin"])

# Admin + Author can delete documents  
deleteDocument(objectKeys: [String!]!): Boolean!
  @aws_auth(cognito_groups: ["Admin", "Author"])

# Queries inherit type-level auth (all authenticated users)
type Query @aws_cognito_user_pools @aws_iam {
  getConfigVersion(versionName: String!): ConfigurationResponse  # No field-level auth
  ...
}
```

### Layer 2: Server-Side Document Filtering (Resolver-Level)

The `listDocuments` Lambda resolver reads the caller's Cognito groups from `$ctx.identity.claims` and applies server-side filtering:

- **Admin/Author/Viewer**: See all documents
- **Reviewer-only**: DynamoDB `FilterExpression` restricts results to:
  - Documents with `HITLTriggered = true` that are pending (not completed/skipped) and either unassigned or assigned to the reviewer
  - Documents where `HITLReviewOwner` matches the reviewer (their own completed reviews)

### Layer 3: UI Adaptation (UX Convenience)

The UI adapts based on the user's role:
- Navigation sidebar shows only relevant features per role
- Action buttons (delete, reprocess, upload) are hidden for roles that can't perform those actions
- The top navigation badge shows the user's role with color coding (blue=Admin, green=Author, grey=Reviewer/Viewer)

**This layer is NOT a security boundary** — it's purely for user experience. Security is enforced at Layers 1 & 2.

## User Management

Admins can create users with any of the four roles via the User Management page. Each user is:
1. Created in DynamoDB (source of truth)
2. Synced to Cognito (for authentication)
3. Added to the appropriate Cognito group (for authorization)

### Config-Version Scoping (Phase 2)

Users can optionally have `allowedConfigVersions` set to restrict their access to specific configuration versions. This is stored in DynamoDB and will be enforced at the resolver level in a future update.

## Architecture

```
┌─────────────────────┐
│  Browser (UI)       │  Layer 3: Navigation/button hiding (UX only)
│  useUserRole hook   │
└────────┬────────────┘
         │ GraphQL
┌────────▼────────────┐
│  AppSync API        │  Layer 1: @aws_auth directives (DENY if wrong group)
│  Schema Directives  │
└────────┬────────────┘
         │
┌────────▼────────────┐
│  Lambda Resolvers   │  Layer 2: Server-side filtering (Reviewer doc filter)
│  listDocuments etc  │
└────────┬────────────┘
         │
┌────────▼────────────┐
│  DynamoDB           │  Data store
│  TrackingTable      │
└─────────────────────┘
```

## Adding New Roles

To add a new role:
1. Add a `AWS::Cognito::UserPoolGroup` in `template.yaml`
2. Add the group name to relevant `@aws_auth` directives in `schema.graphql`
3. Update the `VALID_PERSONAS` dict in `src/lambda/user_management/index.py`
4. Add role detection in `src/ui/src/hooks/use-user-role.ts`
5. Add navigation items in `src/ui/src/components/genaiidp-layout/navigation.tsx`
6. Pass the new group as an environment variable to the UserManagement Lambda
