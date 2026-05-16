use actix_web::{web, Scope};
use actix_web::web::{Data, Json};
use actix_web::Result;
use app_error::ErrorCode;
use database::pg_row::AFWorkspaceMemberRow;
use database::workspace::select_workspace_pending_invitations;
use database_entity::dto::{AFAccessLevel, AFRole};
use shared_entity::{
  dto::guest_dto::{
    RevokeSharedViewAccessRequest, ShareViewWithGuestRequest, SharedUser, SharedViewDetails,
    SharedViewDetailsRequest, SharedViews,
  },
  dto::workspace_dto::WorkspaceMemberInvitation,
  response::{AppResponse, AppResponseError, JsonAppResponse},
};
use sqlx::PgPool;
use tracing::instrument;
use uuid::Uuid;

use crate::biz::authentication::jwt::UserUuid;
use crate::biz::workspace;
use crate::state::AppState;

pub fn sharing_scope() -> Scope {
  web::scope("/api/sharing/workspace")
    .service(
      web::resource("{workspace_id}/view")
        .route(web::get().to(list_shared_views_handler))
        .route(web::put().to(put_shared_view_handler)),
    )
    .service(
      web::resource("{workspace_id}/view/{view_id}/access-details")
        .route(web::post().to(shared_view_access_details_handler)),
    )
    .service(
      web::resource("{workspace_id}/view/{view_id}/revoke-access")
        .route(web::post().to(revoke_shared_view_access_handler)),
    )
}

fn role_to_access_level(role: &AFRole) -> AFAccessLevel {
  match role {
    AFRole::Owner => AFAccessLevel::FullAccess,
    AFRole::Member => AFAccessLevel::ReadAndWrite,
    AFRole::Guest => AFAccessLevel::ReadOnly,
  }
}

fn access_level_to_role(level: &AFAccessLevel) -> AFRole {
  match level {
    AFAccessLevel::FullAccess => AFRole::Member,
    AFAccessLevel::ReadAndWrite => AFRole::Member,
    AFAccessLevel::ReadAndComment => AFRole::Guest,
    AFAccessLevel::ReadOnly => AFRole::Guest,
  }
}

#[instrument(skip_all, err)]
async fn list_shared_views_handler(
  user_uuid: UserUuid,
  state: Data<AppState>,
  workspace_id: web::Path<Uuid>,
) -> Result<JsonAppResponse<SharedViews>> {
  let uid = state.user_cache.get_user_uid(&user_uuid).await?;
  let workspace_id = workspace_id.into_inner();
  state
    .workspace_access_control
    .enforce_role_weak(&uid, &workspace_id, AFRole::Guest)
    .await?;

  Ok(
    AppResponse::Ok()
      .with_data(SharedViews {
        shared_views: vec![],
        view_id_with_no_access: vec![],
      })
      .into(),
  )
}

#[instrument(skip_all, err)]
async fn put_shared_view_handler(
  user_uuid: UserUuid,
  state: Data<AppState>,
  payload: Json<ShareViewWithGuestRequest>,
  workspace_id: web::Path<Uuid>,
) -> Result<JsonAppResponse<()>> {
  let uid = state.user_cache.get_user_uid(&user_uuid).await?;
  let workspace_id = workspace_id.into_inner();
  state
    .workspace_access_control
    .enforce_role_weak(&uid, &workspace_id, AFRole::Member)
    .await?;

  let req = payload.into_inner();
  let role = access_level_to_role(&req.access_level);
  let invitations: Vec<WorkspaceMemberInvitation> = req
    .emails
    .iter()
    .map(|email| WorkspaceMemberInvitation {
      email: email.clone(),
      role: role.clone(),
      skip_email_send: false,
      wait_email_send: true,
    })
    .collect();

  workspace::ops::invite_workspace_members(
    &state.mailer,
    &state.pg_pool,
    &user_uuid,
    &workspace_id,
    invitations,
    &state.config.appflowy_web_url,
  )
  .await?;

  Ok(AppResponse::Ok().into())
}

#[instrument(skip_all, err)]
async fn shared_view_access_details_handler(
  user_uuid: UserUuid,
  state: Data<AppState>,
  _json: Json<SharedViewDetailsRequest>,
  path: web::Path<(Uuid, Uuid)>,
) -> Result<JsonAppResponse<SharedViewDetails>> {
  let uid = state.user_cache.get_user_uid(&user_uuid).await?;
  let (workspace_id, view_id) = path.into_inner();
  state
    .workspace_access_control
    .enforce_role_weak(&uid, &workspace_id, AFRole::Guest)
    .await?;

  let members = select_workspace_member_list_include_guest(&state.pg_pool, &workspace_id).await?;
  let pending = select_workspace_pending_invitations(&state.pg_pool, &workspace_id).await?;

  let shared_with: Vec<SharedUser> = members
    .iter()
    .map(|m| SharedUser {
      view_id,
      email: m.email.clone(),
      name: m.name.clone(),
      access_level: role_to_access_level(&m.role),
      role: m.role.clone(),
      avatar_url: m.avatar_url.clone(),
      pending_invitation: pending.contains_key(&m.email),
    })
    .collect();

  Ok(
    AppResponse::Ok()
      .with_data(SharedViewDetails {
        view_id,
        shared_with,
      })
      .into(),
  )
}

#[instrument(skip_all, err)]
async fn revoke_shared_view_access_handler(
  user_uuid: UserUuid,
  state: Data<AppState>,
  payload: Json<RevokeSharedViewAccessRequest>,
  path: web::Path<(Uuid, Uuid)>,
) -> Result<JsonAppResponse<()>> {
  let uid = state.user_cache.get_user_uid(&user_uuid).await?;
  let (workspace_id, _view_id) = path.into_inner();
  state
    .workspace_access_control
    .enforce_role_strong(&uid, &workspace_id, AFRole::Owner)
    .await?;

  let emails = payload.into_inner().emails;
  workspace::ops::remove_workspace_members(
    &state.pg_pool,
    &workspace_id,
    &emails,
    state.workspace_access_control.clone(),
  )
  .await?;

  Ok(AppResponse::Ok().into())
}

async fn select_workspace_member_list_include_guest(
  pg_pool: &PgPool,
  workspace_id: &Uuid,
) -> Result<Vec<AFWorkspaceMemberRow>, AppResponseError> {
  #[derive(sqlx::FromRow)]
  struct MemberRow {
    uid: i64,
    name: String,
    email: String,
    avatar_url: Option<String>,
    role: i32,
    created_at: Option<chrono::DateTime<chrono::Utc>>,
  }

  let rows: Vec<MemberRow> = sqlx::query_as(
    r#"
    SELECT
      af_user.uid,
      af_user.name,
      af_user.email,
      af_user.metadata ->> 'icon_url' AS avatar_url,
      af_workspace_member.role_id AS role,
      af_workspace_member.created_at
    FROM public.af_workspace_member
        JOIN public.af_user ON af_workspace_member.uid = af_user.uid
    WHERE af_workspace_member.workspace_id = $1
    ORDER BY af_workspace_member.created_at ASC;
    "#,
  )
  .bind(workspace_id)
  .fetch_all(pg_pool)
  .await
  .map_err(|e| {
    AppResponseError::new(
      ErrorCode::Internal,
      format!("Failed to query workspace members: {}", e),
    )
  })?;

  let members = rows
    .into_iter()
    .map(|r| AFWorkspaceMemberRow {
      uid: r.uid,
      name: r.name,
      email: r.email,
      avatar_url: r.avatar_url,
      role: AFRole::from(r.role),
      created_at: r.created_at,
    })
    .collect();

  Ok(members)
}
