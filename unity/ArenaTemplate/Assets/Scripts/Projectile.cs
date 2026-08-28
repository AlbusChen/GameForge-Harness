using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    [RequireComponent(typeof(SphereCollider))]
    [RequireComponent(typeof(Rigidbody))]
    public sealed class Projectile : MonoBehaviour
    {
        private Vector3 direction;
        private int damage;
        private float speed;
        private float remainingRange;

        public static Projectile Create(
            Vector3 position,
            Vector3 direction,
            int damage,
            float speed,
            float range)
        {
            GameObject projectileObject = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            projectileObject.name = "Projectile";
            projectileObject.transform.position = position;
            projectileObject.transform.localScale = Vector3.one * 0.3f;

            SphereCollider collider = projectileObject.GetComponent<SphereCollider>();
            collider.isTrigger = true;
            Rigidbody body = projectileObject.AddComponent<Rigidbody>();
            body.isKinematic = true;
            body.useGravity = false;
            body.collisionDetectionMode = CollisionDetectionMode.ContinuousSpeculative;

            Projectile projectile = projectileObject.AddComponent<Projectile>();
            projectile.direction = direction.normalized;
            projectile.damage = damage;
            projectile.speed = speed;
            projectile.remainingRange = range;
            return projectile;
        }

        private void Update()
        {
            float distance = speed * Time.deltaTime;
            transform.position += direction * distance;
            remainingRange -= distance;
            if (remainingRange <= 0f)
            {
                Destroy(gameObject);
            }
        }

        private void OnTriggerEnter(Collider other)
        {
            if (other.TryGetComponent(out EnemyHealth enemy))
            {
                enemy.TakeDamage(damage);
                Destroy(gameObject);
                return;
            }

            if (!other.TryGetComponent<PlayerHealth>(out _))
            {
                Destroy(gameObject);
            }
        }
    }
}
