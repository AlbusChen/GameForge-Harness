using System.Collections;
using UnityEngine;

namespace VerifiedGameBuilder.Game
{
    [RequireComponent(typeof(Rigidbody))]
    public sealed class PlayerController : MonoBehaviour
    {
        [SerializeField] private float speed = 6f;
        private Rigidbody body;
        private Vector3 testInput;

        private void Awake()
        {
            body = GetComponent<Rigidbody>();
        }

        private void FixedUpdate()
        {
            Vector3 input = testInput;
            if (input.sqrMagnitude < 0.001f)
            {
                input = new Vector3(Input.GetAxisRaw("Horizontal"), 0f, Input.GetAxisRaw("Vertical"));
            }

            Move(input, Time.fixedDeltaTime);
        }

        public void Configure(float movementSpeed)
        {
            speed = Mathf.Max(0f, movementSpeed);
        }

        public void Move(Vector3 direction, float deltaTime)
        {
            if (GameController.Instance.Status != GameStatus.Playing)
            {
                return;
            }

            Vector3 normalized = Vector3.ClampMagnitude(direction, 1f);
            body.MovePosition(body.position + normalized * speed * deltaTime);
        }

        public IEnumerator MoveForTest(Vector3 direction, float duration)
        {
            testInput = Vector3.ClampMagnitude(direction, 1f);
            yield return new WaitForSeconds(duration);
            testInput = Vector3.zero;
        }
    }
}
