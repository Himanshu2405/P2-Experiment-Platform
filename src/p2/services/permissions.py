"""Who may do what. Every service call takes an Actor and checks here first (TECH_SPEC.md section 2).

Reading is open to every active team member. Writing needs a writer role, and is scoped: people work in
their own product and change their own experiments; an admin can do everything.
"""
from dataclasses import dataclass

from p2.services.errors import PermissionDenied

ROLES = ("viewer", "experimenter", "metric_owner", "admin")
WRITER_ROLES = ("experimenter", "metric_owner", "admin")


@dataclass(frozen=True)
class Actor:
    user_id: str
    name: str
    role: str
    product_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def require_admin(actor: Actor) -> None:
    if not actor.is_admin:
        raise PermissionDenied(f"{actor.name} ({actor.role}) needs the admin role for this")


def require_create_experiment(actor: Actor, product_id: str) -> None:
    if actor.role not in WRITER_ROLES:
        raise PermissionDenied(f"{actor.name} ({actor.role}) has read-only access")
    if not actor.is_admin and actor.product_id != product_id:
        raise PermissionDenied(f"{actor.name} works in {actor.product_id}, not {product_id}")


def require_edit_experiment(actor: Actor, owner_user_id: str) -> None:
    if actor.role not in WRITER_ROLES:
        raise PermissionDenied(f"{actor.name} ({actor.role}) has read-only access")
    if not actor.is_admin and actor.user_id != owner_user_id:
        raise PermissionDenied(f"only the owner ({owner_user_id}) or an admin can change this experiment")


def require_propose_item(actor: Actor, product_id: str) -> None:
    if actor.role not in WRITER_ROLES:
        raise PermissionDenied(f"{actor.name} ({actor.role}) has read-only access")
    if not actor.is_admin and actor.product_id != product_id:
        raise PermissionDenied(f"{actor.name} works in {actor.product_id}; only an admin can add items to {product_id}")


def require_edit_item(actor: Actor, author: str) -> None:
    if actor.role not in WRITER_ROLES:
        raise PermissionDenied(f"{actor.name} ({actor.role}) has read-only access")
    if not actor.is_admin and actor.user_id != author:
        raise PermissionDenied(f"only the author ({author}) or an admin can change this item")


def require_review_item(actor: Actor, product_id: str, author: str) -> None:
    """Certify or reject: the product's metric owner, or an admin, and never the author."""
    if actor.role not in ("metric_owner", "admin"):
        raise PermissionDenied(f"{actor.name} ({actor.role}) cannot review catalog items")
    if not actor.is_admin and actor.product_id != product_id:
        raise PermissionDenied(f"{actor.name} owns {actor.product_id} items; this one belongs to {product_id}")
    if actor.user_id == author:
        raise PermissionDenied("separation of duties: the author cannot review their own item")


def require_manage_item(actor: Actor, product_id: str) -> None:
    if actor.role not in ("metric_owner", "admin"):
        raise PermissionDenied(f"{actor.name} ({actor.role}) cannot retire catalog items")
    if not actor.is_admin and actor.product_id != product_id:
        raise PermissionDenied(f"{actor.name} owns {actor.product_id} items; this one belongs to {product_id}")
