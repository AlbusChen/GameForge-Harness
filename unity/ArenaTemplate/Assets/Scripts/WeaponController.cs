using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    public sealed class WeaponController : MonoBehaviour
    {
        [SerializeField] private int damage = 10;
        [SerializeField] private float fireRate = 4f;
        [SerializeField] private float range = 20f;
        [SerializeField] private float projectileSpeed = 18f;

        private Camera gameCamera;
        private float nextShotTime;
        private Vector3? testAimPoint;

        public int ShotsFired { get; private set; }

        private void Awake()
        {
            gameCamera = Camera.main;
        }

        private void Update()
        {
            if (Input.GetMouseButton(0))
            {
                TryFire(ResolveMouseAim());
            }
        }

        public void Configure(int shotDamage, float shotsPerSecond, float weaponRange)
        {
            damage = Mathf.Max(1, shotDamage);
            fireRate = Mathf.Max(0.1f, shotsPerSecond);
            range = Mathf.Max(1f, weaponRange);
        }

        public void SetTestAim(Vector3 point)
        {
            testAimPoint = point;
        }

        public bool FireForTest()
        {
            Vector3 aimPoint = testAimPoint ?? transform.position + transform.forward * range;
            nextShotTime = 0f;
            return TryFire(aimPoint);
        }

        public bool TryFire(Vector3 aimPoint)
        {
            if (GameController.Instance.Status != GameStatus.Playing || Time.time < nextShotTime)
            {
                return false;
            }

            Vector3 origin = transform.position + Vector3.up * 0.35f;
            Vector3 direction = aimPoint - origin;
            direction.y = 0f;
            if (direction.sqrMagnitude < 0.001f)
            {
                return false;
            }

            direction.Normalize();
            Projectile.Create(origin + direction * 0.8f, direction, damage, projectileSpeed, range);
            transform.rotation = Quaternion.LookRotation(direction, Vector3.up);
            nextShotTime = Time.time + 1f / fireRate;
            ShotsFired++;
            return true;
        }

        private Vector3 ResolveMouseAim()
        {
            if (gameCamera == null)
            {
                gameCamera = Camera.main;
            }

            Ray ray = gameCamera.ScreenPointToRay(Input.mousePosition);
            Plane arenaPlane = new Plane(Vector3.up, Vector3.zero);
            return arenaPlane.Raycast(ray, out float distance)
                ? ray.GetPoint(distance)
                : transform.position + transform.forward * range;
        }
    }
}
